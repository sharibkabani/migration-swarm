"""LLM calls: one choke point that meters cost, enforces the budget, and tags every call.

Every call is tagged (LangSmith metadata/tags) with run id, migration, agent role and file. Tracing
is enabled the standard LangChain way, e.g. LANGSMITH_TRACING=true + LANGSMITH_API_KEY.
Rate limits: the Anthropic SDK retries 429/5xx/connection errors with exponential backoff
(`api_max_retries` in config).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from migration_swarm.core.budget import CostMeter, Usage

ChatFactory = Callable[[str, str, int, int], BaseChatModel]  # (model, effort, max_tokens, max_retries)


def anthropic_factory(model: str, effort: str, max_tokens: int, max_retries: int) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic  # reads ANTHROPIC_API_KEY

    name = model.split(":", 1)[-1]
    return ChatAnthropic(model=name, max_tokens=max_tokens, max_retries=max_retries, streaming=True,
                         output_config={"effort": effort})


@dataclass
class LLMResult:
    text: str
    model: str
    cost_usd: float
    usage: Usage
    stop_reason: str | None


class LLM:
    def __init__(self, cfg: dict, meter: CostMeter, run_id: str, migration: str, factory: ChatFactory | None = None):
        self.cfg, self.meter, self.run_id, self.migration = cfg, meter, run_id, migration
        self.factory = factory or anthropic_factory

    def model_for(self, role: str) -> str:
        return self.cfg["models"][role]

    async def complete(self, role: str, system: str, user: str, *, key: str, model_role: str | None = None) -> LLMResult:
        """role: agent role for tagging (worker/escalation/steward/planner); model_role picks the model."""
        model_role = model_role or role
        model = self.model_for(model_role)
        max_out = int(self.cfg["max_output_tokens"])
        est_prompt = (len(system) + len(user)) // 3 + 50
        reservation = self.meter.reserve(model, est_prompt, max_out, key=key)
        chat = self.factory(model, self.cfg["effort"].get(model_role, "medium"), max_out, int(self.cfg["api_max_retries"]))
        messages = [
            SystemMessage(content=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]),
            HumanMessage(content=user),
        ]
        run_config = {
            "run_name": f"{role}:{key}",
            "tags": [f"run:{self.run_id}", f"migration:{self.migration}", f"role:{role}", f"file:{key}"],
            "metadata": {"run_id": self.run_id, "migration": self.migration, "role": role, "file": key, "model": model},
        }
        try:
            msg = await chat.ainvoke(messages, config=run_config)
        except BaseException:
            self.meter.release(reservation)
            raise
        usage = Usage.from_langchain(getattr(msg, "usage_metadata", None))
        cost = self.meter.settle(reservation, model, usage, key=key)
        stop = (getattr(msg, "response_metadata", None) or {}).get("stop_reason")
        return LLMResult(msg.text, model, cost, usage, stop)
