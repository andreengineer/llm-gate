"""Regression tests for I7_MAIN.md fixes (2026-09-28 session).

Context: verified against live code that three real gaps existed:
(1) DeepSeek input was billed entirely at the cache-MISS rate even though
    real traffic is ~99.4% cache hits (I7_MAIN.md §2.2) — massively
    overstating cost and tripping budget caps on spend that never happened.
(2) DeepSeek's own default is thinking ON, silently billing reasoning
    tokens as output at 2-6.6x the base rate (I7_MAIN.md §2.3).
(3) The dedup guard (app/cache.py) still hard-blocked legitimate
    background/cron jobs that paste last run's report into a user turn for
    comparison — a known-open issue (see memory hermes-cron-agents.md) for
    the Weekly Model Review / outcome-agent-weekly-recon jobs specifically.
"""
import json

import httpx
import pytest

from app import ledger
from app.models_registry import price_at, DEEPSEEK_CACHE_HIT_PRICES

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _completion_body(prompt_tokens=1000, completion_tokens=50, cache_hit_tokens=0, model="x"):
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cache_hit_tokens:
        usage["prompt_cache_hit_tokens"] = cache_hit_tokens
        usage["prompt_cache_miss_tokens"] = prompt_tokens - cache_hit_tokens
    return {
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": usage,
        "model": model,
    }


# Reuse the shared test client/header helpers from test_gate.py rather than
# redefining them, to stay pinned to the exact same ASGI app/ports.yaml setup.
from tests.test_gate import _client, HEADERS  # noqa: E402


# --------------------------------------------------------- §2.2 cache split

def test_price_at_returns_distinct_hit_and_miss_for_deepseek_flash():
    miss, hit, out = price_at("deepseek/deepseek-v4-flash", ts=_off_peak_ts())
    assert miss == 0.15
    assert hit == DEEPSEEK_CACHE_HIT_PRICES["deepseek/deepseek-flash"] == 0.003
    assert out == 0.60


def test_price_at_applies_peak_multiplier_to_hit_price_too():
    miss, hit, out = price_at("deepseek/deepseek-v4-flash", ts=_peak_ts())
    assert miss == 0.30
    assert hit == 0.006  # 2x the off-peak 0.003, not left at the off-peak rate
    assert out == 1.20


def test_national_day_holiday_is_off_peak_even_inside_peak_hours():
    import datetime as dt
    from app.models_registry import is_deepseek_peak
    # 2026-10-01 02:00 UTC = 2026-10-01 10:00 CST, inside the 06-10 UTC peak
    # window and on National Day (CST calendar date) -> must be off-peak.
    ts = dt.datetime(2026, 10, 1, 2, 0, tzinfo=dt.timezone.utc).timestamp()
    assert is_deepseek_peak(ts) is False


def test_non_deepseek_model_has_no_cache_discount():
    from app.models_registry import registry, ModelInfo
    registry.models["openrouter/plain"] = ModelInfo("openrouter/plain", 1.0, 2.0, "openrouter")
    try:
        miss, hit, out = price_at("openrouter/plain")
        assert miss == hit == 1.0  # no discount modeled -> hit == miss
        assert out == 2.0
    finally:
        registry.models.pop("openrouter/plain", None)


def _off_peak_ts() -> float:
    import datetime
    # Wednesday 12:00 UTC — outside both peak windows (01-04, 06-10)
    return datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def _peak_ts() -> float:
    import datetime
    # Wednesday 02:00 UTC — inside the 01-04 peak window
    return datetime.datetime(2026, 9, 30, 2, 0, tzinfo=datetime.timezone.utc).timestamp()


@pytest.mark.asyncio
async def test_ledger_bills_cache_hit_tokens_at_the_hit_rate_not_miss_rate(mock_upstreams, monkeypatch):
    import app.models_registry as mr
    monkeypatch.setattr(mr, "is_deepseek_peak", lambda ts=None: False)  # deterministic off-peak

    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=_completion_body(
            prompt_tokens=1_000_000, completion_tokens=0, cache_hit_tokens=990_000,
        ))
    )
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers={"X-Run-Id": "r_cache_split", "X-Agent-Id": "agent_cache_split"})
    assert resp.status_code == 200
    assert ds_route.call_count == 1

    _, cost = ledger.run_id_stats("r_cache_split")
    # 990k hit @ $0.003/M + 10k miss @ $0.15/M = 0.00297 + 0.0015 = 0.00447
    expected = (990_000 / 1_000_000) * 0.003 + (10_000 / 1_000_000) * 0.15
    assert cost == pytest.approx(expected, rel=1e-6)
    # Sanity: the old all-at-miss-rate behavior would have billed ~0.15,
    # i.e. >30x more than the correct cache-aware cost.
    assert cost < expected * 2
    assert cost * 30 < 0.15


# ------------------------------------------------------ §2.3 thinking off

@pytest.mark.asyncio
async def test_thinking_forced_off_by_default_for_deepseek(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 200
    sent_body = json.loads(ds_route.calls[0].request.content)
    assert sent_body["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_x_think_header_leaves_thinking_unset(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers={**HEADERS, "X-Think": "1"})
    assert resp.status_code == 200
    sent_body = json.loads(ds_route.calls[0].request.content)
    assert "thinking" not in sent_body
    assert "_think_requested" not in sent_body  # internal marker must never leak upstream


@pytest.mark.asyncio
async def test_explicit_reasoning_effort_is_never_overridden(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "reasoning_effort": "high",
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 200
    sent_body = json.loads(ds_route.calls[0].request.content)
    assert "thinking" not in sent_body
    assert sent_body["reasoning_effort"] == "high"


# ------------------------------------------------- dedup guard task-class exemption

def _dup_messages():
    doc = "\n".join(f"report line {i}" for i in range(12))
    return [
        {"role": "user", "content": doc},
        {"role": "user", "content": doc + "\nreport line 99"},
    ]


@pytest.mark.asyncio
async def test_dedup_guard_still_blocks_interactive_traffic(mock_upstreams):
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": _dup_messages()},
                             headers=HEADERS)
    assert resp.status_code == 400
    assert "dedup_rejected" in resp.text
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_dedup_guard_exempts_background_task_class(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": _dup_messages()},
                             headers={**HEADERS, "X-Task-Class": "background"})
    assert resp.status_code == 200
    assert ds_route.call_count == 1


@pytest.mark.asyncio
async def test_dedup_guard_exempts_cron_task_class(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": _dup_messages()},
                             headers={**HEADERS, "X-Task-Class": "cron"})
    assert resp.status_code == 200
    assert ds_route.call_count == 1


@pytest.mark.asyncio
async def test_dedup_guard_not_exempted_by_experiment_task_class(mock_upstreams):
    # only background/cron are exempt — experiment is still interactive-risk traffic
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": _dup_messages()},
                             headers={**HEADERS, "X-Task-Class": "experiment"})
    assert resp.status_code == 400
    assert not mock_upstreams.calls
