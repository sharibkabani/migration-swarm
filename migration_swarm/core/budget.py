"""Cost meter: real cost from API-reported token usage x the configured price table.

The API reports tokens, not dollars. Before each call the meter reserves that call's worst case
(prompt estimate + max output at the highest applicable tier), so concurrent calls can never push
spend past the hard budget. After the call the reservation is replaced by the actual cost.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    pass


class TokenCapExceeded(Exception):
    pass


def _model_key(model: str) -> str:
    return model.split(":", 1)[-1]


@dataclass
class Usage:
    input_tokens: int = 0          # uncached input
    output_tokens: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write_5m + self.cache_write_1h

    @classmethod
    def from_langchain(cls, usage_metadata: dict | None) -> "Usage":
        """LangChain's usage_metadata.input_tokens already *includes* cache reads and writes."""
        um = usage_metadata or {}
        d = um.get("input_token_details") or {}
        cr = d.get("cache_read") or 0
        w5 = d.get("ephemeral_5m_input_tokens") or 0
        w1 = d.get("ephemeral_1h_input_tokens") or 0
        generic = d.get("cache_creation") or 0
        if not (w5 or w1):
            w5 = generic
        total_in = um.get("input_tokens") or 0
        return cls(max(total_in - cr - w5 - w1, 0), um.get("output_tokens") or 0, cr, w5, w1)


def price_usage(prices: dict, model: str, u: Usage) -> float:
    p = prices[_model_key(model)]
    lc = p.get("long_context")
    if lc and u.prompt_tokens > lc["over_prompt_tokens"]:
        p = {**p, **lc}
    # providers without explicit cache-write pricing (OpenAI, Gemini implicit caching) bill writes as input
    return (u.input_tokens * p["input"] + u.output_tokens * p["output"] + u.cache_read * p.get("cache_read", p["input"])
            + u.cache_write_5m * p.get("cache_write_5m", p["input"])
            + u.cache_write_1h * p.get("cache_write_1h", p["input"])) / 1_000_000


@dataclass
class CostMeter:
    budget_usd: float
    prices: dict
    per_key_token_cap: int | None = None
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    calls: int = 0
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    by_model: dict = field(default_factory=dict)
    by_key_tokens: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def worst_case(self, model: str, prompt_tokens_est: int, max_output: int) -> float:
        return price_usage(self.prices, model, Usage(input_tokens=prompt_tokens_est, output_tokens=max_output))

    def reserve(self, model: str, prompt_tokens_est: int, max_output: int, key: str | None = None) -> float:
        cost = self.worst_case(model, prompt_tokens_est, max_output)
        with self._lock:
            if key and self.per_key_token_cap is not None:
                if self.by_key_tokens.get(key, 0) + prompt_tokens_est > self.per_key_token_cap:
                    raise TokenCapExceeded(f"{key}: per-file token cap {self.per_key_token_cap} reached")
            if self.spent_usd + self.reserved_usd + cost > self.budget_usd:
                raise BudgetExceeded(
                    f"budget ${self.budget_usd:.2f}: spent ${self.spent_usd:.4f}, in flight ${self.reserved_usd:.4f}, "
                    f"next call worst case ${cost:.4f}")
            self.reserved_usd += cost
        return cost

    def settle(self, reservation: float, model: str, usage: Usage, key: str | None = None) -> float:
        cost = price_usage(self.prices, model, usage)
        with self._lock:
            self.reserved_usd = max(self.reserved_usd - reservation, 0.0)
            self.spent_usd += cost
            self.calls += 1
            self.tokens["input"] += usage.input_tokens
            self.tokens["output"] += usage.output_tokens
            self.tokens["cache_read"] += usage.cache_read
            self.tokens["cache_write"] += usage.cache_write_5m + usage.cache_write_1h
            m = self.by_model.setdefault(_model_key(model), {"calls": 0, "usd": 0.0, "input": 0, "output": 0})
            m["calls"] += 1
            m["usd"] += cost
            m["input"] += usage.prompt_tokens
            m["output"] += usage.output_tokens
            if key:
                self.by_key_tokens[key] = self.by_key_tokens.get(key, 0) + usage.prompt_tokens + usage.output_tokens
        return cost

    def release(self, reservation: float) -> None:
        with self._lock:
            self.reserved_usd = max(self.reserved_usd - reservation, 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {"budget_usd": self.budget_usd, "spent_usd": round(self.spent_usd, 6), "calls": self.calls,
                    "tokens": dict(self.tokens), "by_model": {k: dict(v) for k, v in self.by_model.items()},
                    "by_key_tokens": dict(self.by_key_tokens)}

    def restore(self, snap: dict) -> None:
        self.spent_usd = snap.get("spent_usd", 0.0)
        self.calls = snap.get("calls", 0)
        self.tokens = dict(snap.get("tokens", self.tokens))
        self.by_model = {k: dict(v) for k, v in snap.get("by_model", {}).items()}
        self.by_key_tokens = dict(snap.get("by_key_tokens", {}))
