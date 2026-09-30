"""I7_MAIN.md §2.1/§2.3/§2.4/§2.5/§2.6/§2.7 — canonical flash name, UTC peak
anchors, OpenRouter reasoning off, truncated_reasoning, Retry-After, the
simplified Ladder A, task routing and passive health (2026-09-30 session)."""
import datetime
import json

import httpx
import pytest

from app import health, ledger
from app.config import settings
from app.models_registry import is_deepseek_peak, price_at
from app.upstreams import completion_kind
from tests.test_gate import _client, HEADERS

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"


def _body(content="ok", reasoning=None, prompt_tokens=100, completion_tokens=10):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"id": "c", "choices": [{"message": msg}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _utc(*a) -> float:
    return datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()


# ------------------------------------------------------------- §2.1 pricing

def test_peak_follows_utc_anchors_not_the_old_1630_window():
    assert is_deepseek_peak(_utc(2026, 9, 29, 1, 0))       # Tue 01:00 UTC
    assert is_deepseek_peak(_utc(2026, 9, 29, 9, 59))      # Tue 09:59 UTC
    assert not is_deepseek_peak(_utc(2026, 9, 29, 4, 0))   # Tue 04:00 UTC (gap)
    assert not is_deepseek_peak(_utc(2026, 9, 29, 17, 0))  # old wrong 16:30-00:30 window
    assert not is_deepseek_peak(_utc(2026, 10, 3, 2, 0))   # Saturday


def test_peak_windows_and_holidays_load_from_routing_yaml():
    import app.models_registry as mr
    assert mr.DEEPSEEK_PEAK_WINDOWS_UTC == ((1, 4), (6, 10))
    assert "2026-10-01" in mr.DEEPSEEK_OFF_PEAK_HOLIDAYS_CST
    assert not is_deepseek_peak(_utc(2026, 10, 5, 2, 0))   # Mon, National Day


@pytest.mark.asyncio
async def test_legacy_flash_name_is_stored_canonical_in_ledger(mock_upstreams):
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_body()))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers={**HEADERS, "X-Run-Id": "r_canon"},
                         json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    with ledger._conn() as conn:
        models = {row[0] for row in conn.execute("SELECT model FROM calls WHERE run_id='r_canon'")}
    assert models == {"deepseek/deepseek-flash"}


def test_rung2_openrouter_is_priced_at_openrouter_rates_not_deepseek():
    or_price = price_at("deepseek/deepseek-v4-flash", provider="openrouter")
    assert or_price == (0.09, 0.09, 0.18)   # no implicit cache on rung 2


# ---------------------------------------------------------- §2.3 reasoning

@pytest.mark.asyncio
async def test_openrouter_reasoning_disabled_by_default_and_x_think_respected(mock_upstreams):
    route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    msgs = [{"role": "user", "content": "or"}]
    async with await _client() as c:
        await c.post("/v1/chat/completions", headers=HEADERS,
                     json={"model": "z-ai/glm-4.7-flash", "messages": msgs})
        await c.post("/v1/chat/completions", headers={**HEADERS, "X-Think": "1", "X-Run-Id": "r2"},
                     json={"model": "z-ai/glm-4.7-flash", "messages": msgs + [{"role": "user", "content": "2"}]})
    assert json.loads(route.calls[0].request.content)["reasoning"] == {"enabled": False}
    assert "reasoning" not in json.loads(route.calls[1].request.content)


def test_truncated_reasoning_is_not_empty_and_not_unfunded():
    body = _body(content="", reasoning="thinking thinking")
    assert completion_kind(body) == "truncated_reasoning"
    assert completion_kind(_body(content="")) == "empty"
    assert completion_kind(_body()) == "ok"
    health.record_result("deepseek", 200, body)
    health.record_result("deepseek", 200, body)
    assert health.get("deepseek").status == health.LIVE   # never marked unfunded


@pytest.mark.asyncio
async def test_truncated_reasoning_returned_to_caller_not_escalated(mock_upstreams):
    mock_upstreams.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=_body(content="", reasoning="r")))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers=HEADERS,
                         json={"model": "deepseek/deepseek-flash",
                               "messages": [{"role": "user", "content": "t"}]})
    assert r.status_code == 200
    assert r.json()["x_gate"]["completion"] == "truncated_reasoning"
    assert not or_route.called


# --------------------------------------------------------- §2.4 Retry-After

@pytest.mark.asyncio
async def test_budget_429_carries_retry_after_and_never_fails_over(mock_upstreams, monkeypatch):
    monkeypatch.setattr(settings, "global_daily_hard", 0.0)
    ds = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_body()))
    orr = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers=HEADERS,
                         json={"model": "deepseek/deepseek-flash",
                               "messages": [{"role": "user", "content": "b"}]})
    assert r.status_code == 429
    assert 0 < int(r.headers["retry-after"]) <= 86400
    assert not ds.called and not orr.called


# ------------------------------------------------------ §2.5/§2.6 routing

@pytest.mark.asyncio
async def test_free_rung_is_never_used_for_isaura(mock_upstreams, monkeypatch):
    monkeypatch.setattr(settings, "zai_api_key", "test-zai")
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(500))
    orr = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    zai = mock_upstreams.post(ZAI_URL).mock(return_value=httpx.Response(200, json=_body()))
    # chat.py sets client_facing=True for isaura; exercise dispatch directly,
    # with a background task class that would otherwise route free-first.
    from app.upstreams import dispatch, NoLiveRouteError
    async with httpx.AsyncClient() as client:
        with pytest.raises(NoLiveRouteError):
            await dispatch(client, "deepseek/deepseek-flash",
                           {"model": "deepseek/deepseek-flash", "messages": [{"role": "user", "content": "i"}]},
                           client_facing=True, task_class="background")
    assert not zai.called and not orr.called   # isaura: rung 1 only


@pytest.mark.asyncio
async def test_background_traffic_tries_free_zai_first(mock_upstreams, monkeypatch):
    monkeypatch.setattr(settings, "zai_api_key", "test-zai")
    ds = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_body()))
    zai = mock_upstreams.post(ZAI_URL).mock(return_value=httpx.Response(200, json=_body()))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers={**HEADERS, "X-Task-Class": "cron"},
                         json={"model": "deepseek/deepseek-flash",
                               "messages": [{"role": "user", "content": "digest"}]})
    assert r.status_code == 200
    assert zai.call_count == 1 and not ds.called
    sent = json.loads(zai.calls[0].request.content)
    assert sent["model"] == "glm-4.7-flash" and sent["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_oneshot_goes_to_rung2_pinned_providers(mock_upstreams):
    ds = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_body()))
    orr = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers={**HEADERS, "X-Task-Class": "oneshot"},
                         json={"model": "deepseek/deepseek-flash",
                               "messages": [{"role": "user", "content": "one"}]})
    assert r.status_code == 200
    assert orr.call_count == 1 and not ds.called
    sent = json.loads(orr.calls[0].request.content)
    assert sent["provider"] == {"order": ["DeepInfra", "Novita"], "allow_fallbacks": False}


@pytest.mark.asyncio
async def test_oneshot_does_not_qualify_for_free_tier_models(mock_upstreams):
    from app.models_registry import registry, ModelInfo
    registry.models["z-ai/glm-4.7-flash:free"] = ModelInfo("z-ai/glm-4.7-flash:free", 0, 0, "openrouter")
    try:
        async with await _client() as c:
            r = await c.post("/v1/chat/completions", headers={**HEADERS, "X-Task-Class": "oneshot"},
                             json={"model": "z-ai/glm-4.7-flash:free",
                                   "messages": [{"role": "user", "content": "f"}]})
        assert r.status_code == 403
    finally:
        registry.models.pop("z-ai/glm-4.7-flash:free", None)


@pytest.mark.asyncio
async def test_rung2_skipped_once_its_daily_cap_is_spent(mock_upstreams):
    ledger.record_call("r_cap", "a", "deepseek/deepseek-flash", "openrouter", "cheap", 0.31, "h")
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(500))
    orr = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    mock_upstreams.post(url__regex=r"https://api\.telegram\.org/.*").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    async with await _client() as c:
        r = await c.post("/v1/chat/completions", headers=HEADERS,
                         json={"model": "deepseek/deepseek-flash",
                               "messages": [{"role": "user", "content": "cap"}]})
    assert r.status_code == 503
    assert not orr.called
    assert any(s["result"] == "rung_cap" for s in r.json()["skipped_by_health_cache"])


# --------------------------------------------------------- §2.7 passive health

@pytest.mark.asyncio
async def test_probe_only_touches_dead_providers(mock_upstreams, monkeypatch):
    monkeypatch.setattr(settings, "zai_api_key", "test-zai")
    ds = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_body()))
    orr = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_body()))
    zai = mock_upstreams.post(ZAI_URL).mock(return_value=httpx.Response(200, json=_body()))
    health.mark("openrouter", health.RATE_LIMITED, "429")
    health.mark("zai", health.LIVE, "ok")
    async with httpx.AsyncClient() as client:
        await health.probe_once(client)
    assert orr.call_count == 1            # dead -> probed, and recovers
    assert health.get("openrouter").status == health.LIVE
    assert not ds.called                  # unknown -> not probed
    assert not zai.called                 # healthy free rung -> never probed
