"""LLM calls: one choke point that meters cost, enforces the budget, and tags every call.

Every call is tagged (LangSmith metadata/tags) with run id, migration, agent role and file. Tracing
is enabled the standard LangChain way, e.g. LANGSMITH_TRACING=true + LANGSMITH_API_KEY.
Rate limits: the Anthropic SDK retries 429/5xx/connection errors with exponential backoff
(`api_max_retries` in config).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from migration_swarm.core import plan_llm, providers
from migration_swarm.core.budget import CostMeter, Usage

ChatFactory = Callable[[str, str, int, int], BaseChatModel]  # (model, effort, max_tokens, max_retries)


def anthropic_factory(model: str, effort: str, max_tokens: int, max_retries: int) -> BaseChatModel:
    """Kept for compatibility; builds the right chat model for any provider prefix."""
    return providers.chat_model(model, effort, max_tokens, max_retries)


def _stop_reason(msg) -> str | None:
    """Normalize provider stop reasons; the graph only cares about 'max_tokens' (output truncated)."""
    md = getattr(msg, "response_metadata", None) or {}
    raw = md.get("stop_reason") or md.get("finish_reason")
    if raw is None:
        return None
    return "max_tokens" if str(raw).lower() in ("max_tokens", "length", "finishreason.max_tokens") else str(raw)


@dataclass
class LLMResult:
    text: str
    model: str
    cost_usd: float
    usage: Usage
    stop_reason: str | None


class LLM:
    def __init__(self, cfg: dict, meter: CostMeter, run_id: str, migration: str, factory: ChatFactory | None = None,
                 *, plan_caller=None, log: Callable[[str], None] | None = None,
                 emit: Callable[..., None] | None = None):
        self.cfg, self.meter, self.run_id, self.migration = cfg, meter, run_id, migration
        self.factory = factory or anthropic_factory
        self.backend = cfg.get("llm_backend", "api")
        self.plan_caller = plan_caller or plan_llm.plan_call
        self.log = log or (lambda m: None)
        self.emit = emit or (lambda kind, **d: None)
        self.paused_s = 0.0          # wall time spent waiting for plan limits to reset
        self._pause_until = 0.0
        if self.backend == "plan" and plan_caller is None:
            plan_llm.disable_api_key()
        elif self.backend not in ("api", "plan"):
            raise ValueError(f"llm_backend must be 'api' or 'plan', not {self.backend!r}")

    def model_for(self, role: str) -> str:
        return self.cfg["models"][role]

    async def complete(self, role: str, system: str, user: str, *, key: str, model_role: str | None = None,
                       expected_output_chars: int | None = None) -> LLMResult:
        """role: agent role for tagging (worker/escalation/steward/planner); model_role picks the model.

        expected_output_chars sizes max_tokens to the job (room for thinking + ~2.5x the expected text),
        which keeps the worst-case budget reservation realistic. Capped by config max_output_tokens.
        """
        model_role = model_role or role
        model = self.model_for(model_role)
        max_out = int(self.cfg["max_output_tokens"])
        if expected_output_chars is not None:
            max_out = min(max_out, 8000 + int(expected_output_chars / 3 * 2.5))
        est_prompt = (len(system) + len(user)) // 3 + 50
        effort = self.cfg["effort"].get(model_role, "medium")
        if self.backend == "plan":
            return await self._complete_plan(model, effort, max_out, est_prompt, system, user, key)
        reservation = self.meter.reserve(model, est_prompt, max_out, key=key)
        chat = self.factory(model, effort, max_out, int(self.cfg["api_max_retries"]))
        sys_msg = (SystemMessage(content=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])
                   if providers.supports_cache_control(model) else SystemMessage(content=system))
        messages = [sys_msg, HumanMessage(content=user)]
        run_config = {
            "run_name": f"{role}:{key}",
            "tags": [f"run:{self.run_id}", f"migration:{self.migration}", f"role:{role}", f"file:{key}"],
            "metadata": {"run_id": self.run_id, "migration": self.migration, "role": role, "file": key, "model": model},
        }
        try:
            msg, last = None, 0.0
            async for chunk in chat.astream(messages, config=run_config):
                msg = chunk if msg is None else msg + chunk
                if (now := time.monotonic()) - last > 0.4:
                    last = now
                    self._progress(key, msg.text)
            if msg is None:
                raise RuntimeError(f"{model} returned an empty stream")
        except BaseException:
            self.meter.release(reservation)
            raise
        usage = Usage.from_langchain(getattr(msg, "usage_metadata", None))
        cost = self.meter.settle(reservation, model, usage, key=key)
        return LLMResult(msg.text, model, cost, usage, _stop_reason(msg))

    def _progress(self, key: str, text: str) -> None:
        tail = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
        self.emit("agent_progress", agent=key, chars=len(text), tail=tail[:160])

    async def _complete_plan(self, model: str, effort: str, max_out: int, est_prompt: int, system: str, user: str,
                             key: str) -> LLMResult:
        plan_cfg = self.cfg.get("plan") or {}
        max_wait = float(plan_cfg.get("max_wait_s", 6 * 3600))
        poll = float(plan_cfg.get("poll_s", 900))
        while True:
            if (delay := self._pause_until - time.time()) > 0:
                await asyncio.sleep(delay)
            reservation = self.meter.reserve(model, est_prompt, max_out, key=key)
            try:
                reply = await self.plan_caller(model, effort, max_out, system, user,
                                               on_text=lambda t: self._progress(key, t))
            except plan_llm.PlanRateLimited as e:
                self.meter.release(reservation)
                now = time.time()
                until = max(e.resets_at + 30 if e.resets_at else now + poll, now + 60)
                if until > self._pause_until:
                    self.paused_s += until - max(self._pause_until, now)
                    self._pause_until = until
                if self.paused_s > max_wait:
                    raise plan_llm.PlanLimitReached(f"{e}; waited {self.paused_s / 3600:.1f}h in total, over plan.max_wait_s")
                self.log(f"plan limit: {e}; pausing calls until {time.strftime('%H:%M', time.localtime(self._pause_until))}")
                self.emit("plan_wait", until=self._pause_until, reason=str(e))
                continue
            except BaseException:
                self.meter.release(reservation)
                raise
            cost = self.meter.settle(reservation, model, reply.usage, key=key)
            return LLMResult(reply.text, model, cost, reply.usage, reply.stop_reason)
