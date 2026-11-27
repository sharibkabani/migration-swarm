import threading

import pytest

from migration_swarm.core import config
from migration_swarm.core.budget import BudgetExceeded, CostMeter, TokenCapExceeded, Usage, price_usage

PRICES = config.load()["prices"]


def test_haiku_long_context_tier_applies_per_request_including_cache():
    small = Usage(input_tokens=50_000, output_tokens=1_000, cache_read=40_000)
    big = Usage(input_tokens=50_000, output_tokens=1_000, cache_read=60_000)  # 110k prompt > 100k
    assert price_usage(PRICES, "anthropic:claude-haiku-5-5", small) == pytest.approx((50_000 * 0.10 + 1_000 * 0.50 + 40_000 * 0.01) / 1e6)
    assert price_usage(PRICES, "anthropic:claude-haiku-5-5", big) == pytest.approx((50_000 * 0.50 + 1_000 * 2.50 + 60_000 * 0.05) / 1e6)


def test_usage_from_langchain_separates_cached_input():
    u = Usage.from_langchain({"input_tokens": 1000, "output_tokens": 10,
                              "input_token_details": {"cache_read": 600, "cache_creation": 0,
                                                      "ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 0}})
    assert (u.input_tokens, u.cache_read, u.cache_write_5m, u.prompt_tokens) == (100, 600, 300, 1000)


def test_reservations_enforce_hard_budget_under_concurrency():
    meter = CostMeter(budget_usd=0.01, prices=PRICES)
    ok, refused = [], []

    def call():
        try:
            r = meter.reserve("claude-sonnet-5-5", 1_000, 500)  # worst case $0.007
            ok.append(r)
        except BudgetExceeded:
            refused.append(1)

    threads = [threading.Thread(target=call) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1 and len(refused) == 7
    meter.settle(ok[0], "claude-sonnet-5-5", Usage(input_tokens=1_000, output_tokens=100), key="a.js")
    assert meter.spent_usd == pytest.approx((1_000 * 2 + 100 * 10) / 1e6)
    assert meter.reserved_usd == 0


def test_per_file_token_cap():
    meter = CostMeter(budget_usd=100, prices=PRICES, per_key_token_cap=1_000)
    r = meter.reserve("claude-haiku-5-5", 800, 100, key="a.js")
    meter.settle(r, "claude-haiku-5-5", Usage(input_tokens=800, output_tokens=100), key="a.js")
    with pytest.raises(TokenCapExceeded):
        meter.reserve("claude-haiku-5-5", 800, 100, key="a.js")
