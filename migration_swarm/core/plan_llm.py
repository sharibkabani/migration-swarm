"""`llm_backend: plan` — model calls through the Claude Agent SDK, signed in with a Claude plan.

Each call is one Claude Code turn with no tools, no settings/CLAUDE.md, our own system prompt and no
session persistence, run in an empty directory. Usage draws from the plan's limits instead of being
billed (https://support.claude.com/en/articles/15036540). Auth: the host's Claude Code login or
CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`). ANTHROPIC_API_KEY is removed from the process
environment in this mode so nothing silently falls back to billed API calls.

Cost: the SDK reports token usage; the meter prices it with the same table as the API backend, so
$ figures stay comparable ("API-equivalent" cost). Nothing is charged.

Plan limits: when a call is rejected for a usage limit, the caller raises PlanRateLimited with the
window's reset time; LLM waits for it (all concurrent calls share one pause) up to
`plan.max_wait_s`, then stops the run as `plan_limit`, which `migration-swarm resume` continues.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass

from migration_swarm.core.budget import BudgetExceeded, Usage


class PlanRateLimited(Exception):
    def __init__(self, resets_at: float | None, limit_type: str | None, detail: str = ""):
        super().__init__(f"plan usage limit ({limit_type or 'unknown window'}); resets at "
                         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(resets_at)) if resets_at else 'unknown'} {detail}".strip())
        self.resets_at, self.limit_type = resets_at, limit_type


class PlanLimitReached(BudgetExceeded):
    """The plan's limit was hit and the reset is further away than plan.max_wait_s allows."""


@dataclass
class PlanReply:
    text: str
    usage: Usage
    stop_reason: str | None
    model_usage: dict | None = None


def usage_from_sdk(u: dict | None) -> Usage:
    """Claude Code's result usage: input_tokens excludes cache reads/writes (Messages API shape)."""
    u = u or {}
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0
    if not (w5 or w1):
        w5 = u.get("cache_creation_input_tokens") or 0
    return Usage(u.get("input_tokens") or 0, u.get("output_tokens") or 0, u.get("cache_read_input_tokens") or 0, w5, w1)


_CWD: str | None = None


def _cwd() -> str:
    global _CWD
    if _CWD is None:
        _CWD = tempfile.mkdtemp(prefix="msw-plan-")
    return _CWD


def disable_api_key() -> None:
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)


async def plan_call(model: str, effort: str, max_tokens: int, system: str, user: str) -> PlanReply:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, RateLimitEvent, ResultMessage, TextBlock,
                                  query)

    opts = ClaudeAgentOptions(
        tools=[], system_prompt=system, model=model.split(":", 1)[-1], max_turns=1, effort=effort,
        setting_sources=[], cwd=_cwd(), extra_args={"no-session-persistence": None},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens), "DISABLE_AUTOUPDATER": "1",
             "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    texts: list[str] = []
    rejected = None
    result = None
    async for m in query(prompt=user, options=opts):
        if isinstance(m, AssistantMessage):
            texts += [b.text for b in m.content if isinstance(b, TextBlock)]
        elif isinstance(m, RateLimitEvent) and m.rate_limit_info.status == "rejected":
            rejected = m.rate_limit_info
        elif isinstance(m, ResultMessage):
            result = m
    if result is None:
        raise RuntimeError("Claude Agent SDK returned no result")
    if result.is_error:
        msg = (result.result or "") + " ".join(result.errors or [])
        if rejected is not None or result.api_error_status == 429 or any(
                w in msg.lower() for w in ("usage limit", "rate limit", "hit your limit", "limit reached")):
            ri = rejected
            raise PlanRateLimited(ri.resets_at if ri else None, ri.rate_limit_type if ri else None, msg[:200])
        raise RuntimeError(f"Claude Agent SDK error ({result.subtype}, status {result.api_error_status}): {msg[:500]}")
    return PlanReply("".join(texts) or (result.result or ""), usage_from_sdk(result.usage), result.stop_reason,
                     result.model_usage)
