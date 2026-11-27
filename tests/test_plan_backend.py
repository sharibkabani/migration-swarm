"""Plan backend (Claude Agent SDK on a Claude plan): metering, usage-limit pauses, and giving up."""

import asyncio
import time

import pytest

from migration_swarm.core import config, plan_llm
from migration_swarm.core.budget import CostMeter, Usage
from migration_swarm.core.llm import LLM
from migration_swarm.migrations.js_to_ts import claude_baselines as cb


def _llm(caller, **plan):
    cfg = config.load(llm_backend="plan", plan={"max_wait_s": 5, "poll_s": 1, **plan})
    meter = CostMeter(budget_usd=1.0, prices=cfg["prices"], per_key_token_cap=10**9)
    return LLM(cfg, meter, "r", "js-to-ts", plan_caller=caller), meter


def test_plan_call_is_metered_at_api_prices():
    async def caller(model, effort, max_tokens, system, user):
        return plan_llm.PlanReply("ok", Usage(input_tokens=1000, output_tokens=2000), "end_turn")

    llm, meter = _llm(caller)
    r = asyncio.run(llm.complete("worker", "sys", "user", key="a"))
    assert r.text == "ok" and r.stop_reason == "end_turn"
    # haiku 5.5: $0.10/M in, $0.50/M out
    assert r.cost_usd == pytest.approx(1000 * 0.10 / 1e6 + 2000 * 0.50 / 1e6)
    assert meter.snapshot()["spent_usd"] == pytest.approx(r.cost_usd)


def test_usage_limit_pauses_all_calls_then_retries(monkeypatch):
    calls = []

    async def caller(model, effort, max_tokens, system, user):
        calls.append(time.time())
        if len(calls) == 1:
            raise plan_llm.PlanRateLimited(time.time() - 100, "five_hour")  # reset already passed -> min wait
        return plan_llm.PlanReply("ok", Usage(input_tokens=10, output_tokens=10), "end_turn")

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    llm, meter = _llm(caller, max_wait_s=3600)
    r = asyncio.run(llm.complete("worker", "s", "u", key="a"))
    assert r.text == "ok" and len(calls) == 2
    assert slept and 55 <= slept[0] <= 61          # waits at least 60s
    assert 55 <= llm.paused_s <= 61
    assert meter.snapshot()["calls"] == 1          # the rejected call was released, not billed


def test_gives_up_past_max_wait():
    async def caller(*a):
        raise plan_llm.PlanRateLimited(time.time() + 10 * 3600, "seven_day")

    llm, meter = _llm(caller, max_wait_s=3600)
    with pytest.raises(plan_llm.PlanLimitReached):
        asyncio.run(llm.complete("worker", "s", "u", key="a"))
    assert meter.snapshot()["spent_usd"] == 0


def test_usage_from_sdk_splits_cache():
    u = plan_llm.usage_from_sdk({"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 100,
                                 "cache_creation_input_tokens": 40,
                                 "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 10}})
    assert (u.input_tokens, u.output_tokens, u.cache_read, u.cache_write_5m, u.cache_write_1h) == (5, 7, 100, 30, 10)


def test_baseline_stream_parsing_and_limit_detection():
    out = "\n".join([
        "npm warn something",
        '{"type":"system","subtype":"init"}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","resetsAt":1}}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1800000000,"rateLimitType":"five_hour"}}',
        '{"type":"result","subtype":"success","is_error":true,"result":"You\'ve hit your limit","session_id":"s"}',
    ])
    result, rejected = cb._parse_stream(out)
    assert result["session_id"] == "s" and rejected["resetsAt"] == 1800000000
    assert cb._limited(result, rejected)
    assert not cb._limited({"is_error": True, "subtype": "error_max_budget_usd", "result": ""}, None)
    assert not cb._limited({"is_error": False}, rejected)


def test_baseline_auth_modes(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("MSW_BASELINE_ANTHROPIC_API_KEY", "k")
    assert cb._auth({"llm_backend": "api"}) == ({"ANTHROPIC_API_KEY": "k"}, ["--bare"])
    with pytest.raises(RuntimeError, match="setup-token"):
        cb._auth({"llm_backend": "plan"})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert cb._auth({"llm_backend": "plan"}) == ({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, [])
