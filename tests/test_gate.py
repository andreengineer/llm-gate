import json
import pathlib
import time

import httpx
import pytest
import respx

from app import ledger
from app.config import settings
from app.halt import halt, unlock
from app.main import app
from app.models_registry import registry

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TELEGRAM_SEND = f"https://api.telegram.org/bot{settings.telegram_approval_bot_token}/sendMessage"
TELEGRAM_UPDATES = f"https://api.telegram.org/bot{settings.telegram_approval_bot_token}/getUpdates"


def _completion_body(model="x", prompt_tokens=100, completion_tokens=50):
    return {
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "model": model,
    }


async def _client(port: int = 8787):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://gate:{port}")


HEADERS = {"X-Run-Id": "r_test"}


@pytest.mark.asyncio
async def test_openrouter_auto_denied(mock_upstreams):
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions", json={"model": "openrouter/auto", "messages": []}, headers=HEADERS)
    assert resp.status_code == 403
    assert not mock_upstreams.calls  # zero upstream calls


@pytest.mark.asyncio
async def test_perplexity_sonar_denied(mock_upstreams):
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions", json={"model": "perplexity/sonar-small", "messages": []}, headers=HEADERS)
    assert resp.status_code == 403
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_online_and_extended_suffixes_denied(mock_upstreams):
    async with await _client() as c:
        r1 = await c.post("/v1/chat/completions", json={"model": "openai/gpt-5:online", "messages": []}, headers=HEADERS)
        r2 = await c.post("/v1/chat/completions", json={"model": "anthropic/claude:extended", "messages": []}, headers=HEADERS)
    assert r1.status_code == 403 and r2.status_code == 403


@pytest.mark.asyncio
async def test_ui_only_model_403_with_directive(mock_upstreams):
    async with await _client() as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "google/gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}]},
            headers=HEADERS,
        )
    assert resp.status_code == 403
    assert "claude.ai (sunk)" in resp.text
    assert "ChatGPT or Claude Code" in resp.text
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_mid_model_approval_timeout_denies_zero_spend(mock_upstreams):
    mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    mock_upstreams.get(TELEGRAM_UPDATES).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    async with await _client() as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "anthropic/claude-3.5-haiku", "messages": [{"role": "user", "content": "reason about x"}]},
            headers={"X-Run-Id": "r_mid1", "X-Agent-Id": "agent_mid"},
        )
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "approval_timeout"
    assert not any(str(call.request.url) in (DEEPSEEK_URL, OPENROUTER_URL) for call in mock_upstreams.calls)
    # Spend contract: an approval-timeout denial costs exactly $0.00. The
    # observability change set (2026-09-16) ALSO writes a $0 bookkeeping `calls`
    # row (tier="rejected", outcome="rejected:approval_timeout") so the silence
    # canary can see enforced blocks — an agent whose calls are all
    # "rejected:..." has made zero successful model calls. So assert the spend
    # contract + the new traceability, not the pre-observability "no row at all".
    calls, cost = ledger.run_id_stats("r_mid1")
    assert cost == 0
    assert calls == 1
    assert ledger.recent_rejections(since_hours=1.0)[0][3] == "approval_timeout"
    with ledger._conn() as c:
        row = c.execute(
            "SELECT tier, outcome FROM calls WHERE run_id = ?", ("r_mid1",)
        ).fetchone()
    assert row == ("rejected", "rejected:approval_timeout")


def test_mid_tier_daily_cap_blocks_mid_not_cheap():
    ledger.record_call("seed", "agent_seed_mid", "anthropic/claude-3.5-haiku", "openrouter", "mid", 0.60, "h1")
    mid_decision = ledger.check_budgets("agentX", "r_new", "mid", 0.01, "h2")
    cheap_decision = ledger.check_budgets("agentX", "r_new2", "cheap", 0.01, "h3")
    assert mid_decision.allowed is False
    assert mid_decision.error_code == "mid_tier_cap"
    assert cheap_decision.allowed is True


def test_global_daily_soft_then_hard():
    # spread across many agents/run_ids so per-agent/per-run caps don't mask
    # the global aggregate cap being tested here
    for i in range(8):
        ledger.record_call(f"r_g{i}", f"agent_g{i}", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 0.20, f"hg{i}")
    # soft ($1.50) already crossed (8 x 0.20 = 1.60); still allowed until hard ($2.00)
    d = ledger.check_budgets("agent_g_new", "r_soft", "cheap", 0.05, "h5")
    assert d.allowed is True
    assert ledger.global_daily_spend() >= settings.global_daily_soft

    ledger.record_call("seed3", "agent_g_extra", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 0.50, "h6")
    d2 = ledger.check_budgets("agent_g_final", "r_hard", "cheap", 0.01, "h7")
    assert d2.allowed is False
    assert d2.error_code == "global_daily_hard"


def test_101st_call_same_run_id_blocked():
    for i in range(100):
        ledger.record_call("r_loop", "agentZ", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 0.0001, f"unique{i}")
    d = ledger.check_budgets("agentZ", "r_loop", "cheap", 0.0001, "unique_final")
    assert d.allowed is False
    assert d.error_code == "run_budget_killed"


def test_repeat_prompt_hash_loop_detected():
    # ROUTING_RESILIENCE §2: a chain that exceeds chain_max_attempts (6) is a
    # real loop. 6 prior attempts in the chain -> the 7th trips loop_detected.
    for i in range(settings.chain_max_attempts):
        ledger.record_call(f"r_rep{i}", "agentR", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 0.0001, "same_hash")
    d = ledger.check_budgets("agentR", "r_rep_new", "cheap", 0.0001, "same_hash")
    assert d.allowed is False
    assert d.error_code == "loop_detected"


def test_per_agent_hourly_cap():
    ledger.record_call("r_hr", "agentH", "deepseek/deepseek-v4-flash", "deepseek", "cheap",
                        settings.per_agent_hourly_usd, "hh1")
    d = ledger.check_budgets("agentH", "r_hr2", "cheap", 0.01, "hh2")
    assert d.allowed is False
    assert d.error_code == "agent_hourly_cap"


def test_review_agent_hourly_cap_isolated_from_hermes():
    # background_review.py routes through the dedicated "hermes-review" port
    # (ports.yaml :8793) specifically so a stuck review turn's spend can never
    # exhaust hermes's own bucket, and vice versa — this is the isolation the
    # 2026-09-17 incident was missing.
    ledger.record_call("r_rev", "hermes-review", "deepseek/deepseek-v4-flash", "deepseek", "cheap",
                        settings.review_agent_hourly_usd, "rev1")
    d_review = ledger.check_budgets("hermes-review", "r_rev2", "cheap", 0.01, "rev2")
    assert d_review.allowed is False
    assert d_review.error_code == "agent_hourly_cap"

    d_hermes = ledger.check_budgets("hermes", "r_h2", "cheap", 0.01, "h2")
    assert d_hermes.allowed is True


@pytest.mark.asyncio
async def test_missing_run_id_400_on_manual_port(mock_upstreams):
    # port 8787 ("manual") requires X-Run-Id explicitly — no port-based fallback
    async with await _client(8787) as c:
        resp = await c.post("/v1/chat/completions", json={"model": "deepseek/deepseek-v4-flash", "messages": []})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_isaura_port_no_headers_synthesizes_identity(mock_upstreams):
    # GATE_MIGRATION_PLAN.md Step 2: request to 8788 with no headers -> 200
    # with synthesized agent_id/run_id, port alone is the identity
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "system", "content": "isaura system prompt"},
                                                {"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.headers["X-Gate-Agent-Id"] == "isaura"
    assert resp.headers["X-Gate-Run-Id"].startswith("isaura_")


@pytest.mark.asyncio
async def test_synthesized_run_id_still_enforces_per_run_cap(mock_upstreams):
    # dcode (8791) is a no-header-required port that's still in full
    # "enforce" mode (isaura/8788 and hermes/8789 moved to log_only) — same
    # system prompt within the same hour -> same synthesized run_id -> per
    # run cap must still fire, proving a meterless port isn't unmetered
    import app.identity as identity_mod
    synthetic_run_id = identity_mod.synthesize_run_id("dcode", [{"role": "system", "content": "dcode system prompt"}])
    for i in range(100):
        ledger.record_call(synthetic_run_id, "dcode", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 0.0001, f"v{i}")

    async with await _client(8791) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "system", "content": "dcode system prompt"},
                                                {"role": "user", "content": "another call"}]})
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "run_budget_killed"


@pytest.mark.asyncio
async def test_synthesized_run_id_still_enforces_loop_detection(mock_upstreams):
    # temperature>0.3 makes this non-cacheable, so all 6 calls actually reach
    # the ledger instead of the first one satisfying the rest from cache —
    # a cache hit is zero-cost/zero-upstream already, so it correctly does
    # NOT count toward loop detection (that mechanism exists to catch costly
    # repeats, not free ones)
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    body = {"model": "deepseek/deepseek-v4-flash", "temperature": 0.9,
            "messages": [{"role": "system", "content": "loop test prompt"}, {"role": "user", "content": "identical"}]}
    async with await _client(8791) as c:
        # chain_max_attempts (6) identical attempts are allowed; the next trips it
        for _ in range(settings.chain_max_attempts):
            r = await c.post("/v1/chat/completions", json=body)
            assert r.status_code == 200
        r_over = await c.post("/v1/chat/completions", json=body)
    assert r_over.status_code == 429
    assert r_over.json()["detail"]["error"] == "loop_detected"


@pytest.mark.asyncio
async def test_hermes_port_no_headers_synthesizes_identity(mock_upstreams):
    # hermes ended up on Path B (synthesized run_id) too — no config-only
    # header-injection mechanism was found in hermes-agent's 100+ module
    # codebase, so forcing require_run_id:true would 400 every real request
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client(8789) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "system", "content": "hermes system prompt"},
                                                {"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert resp.headers["X-Gate-Agent-Id"] == "hermes"
    assert resp.headers["X-Gate-Run-Id"].startswith("hermes_")


@pytest.mark.asyncio
async def test_unknown_port_rejected(mock_upstreams):
    async with await _client(9999) as c:
        resp = await c.post("/v1/chat/completions", json={"model": "deepseek/deepseek-v4-flash", "messages": []})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_port"


@pytest.mark.asyncio
async def test_halt_file_503(mock_upstreams):
    halt()
    try:
        async with await _client() as c:
            resp = await c.post("/v1/chat/completions", json={"model": "deepseek/deepseek-v4-flash", "messages": []},
                                 headers=HEADERS)
        assert resp.status_code == 503
    finally:
        unlock()


@pytest.mark.asyncio
async def test_identical_request_twice_cache_hit_zero_upstream(mock_upstreams):
    route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    body = {"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "cache me please"}]}
    async with await _client() as c:
        r1 = await c.post("/v1/chat/completions", json=body, headers={"X-Run-Id": "r_c1", "X-Agent-Id": "agent_cache"})
        assert r1.status_code == 200
        assert route.call_count == 1

        r2 = await c.post("/v1/chat/completions", json=body, headers={"X-Run-Id": "r_c2", "X-Agent-Id": "agent_cache"})
        assert r2.status_code == 200
        assert r2.headers.get("X-Gate-Cache") == "hit"
        assert route.call_count == 1  # unchanged — zero new upstream calls


@pytest.mark.asyncio
async def test_deepseek_prefix_routes_to_deepseek_gemini_routes_to_openrouter(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        r1 = await c.post("/v1/chat/completions",
                           json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                           headers={"X-Run-Id": "r_route1", "X-Agent-Id": "agent_route"})
        r2 = await c.post("/v1/chat/completions",
                           json={"model": "google/gemini-2.5-flash", "messages": [{"role": "user", "content": "b"}]},
                           headers={"X-Run-Id": "r_route2", "X-Agent-Id": "agent_route"})
    assert r1.status_code == 200 and ds_route.call_count == 1
    assert r2.status_code == 200 and or_route.call_count == 1


@pytest.mark.asyncio
async def test_deepseek_503_falls_back_once_to_openrouter_cheap_and_logs_hop(mock_upstreams):
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                             headers={"X-Run-Id": "r_fb1", "X-Agent-Id": "agent_fb"})
    assert resp.status_code == 200
    assert or_route.call_count == 1
    calls, _ = ledger.run_id_stats("r_fb1")
    assert calls == 1


@pytest.mark.asyncio
async def test_deepseek_402_no_balance_surfaces_as_error_not_silent_fallback(mock_upstreams):
    # regression: a non-transient client error (bad key, no balance, ...) must
    # NOT trigger the fallback hop — that would silently mask a billing/auth
    # problem behind a working-looking 200 from OpenRouter instead
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(402, json={"error": "Insufficient Balance"}))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                             headers={"X-Run-Id": "r_402", "X-Agent-Id": "agent_402"})
    assert resp.status_code == 402
    assert or_route.call_count == 0
    calls, _ = ledger.run_id_stats("r_402")
    assert calls == 0


@pytest.mark.asyncio
async def test_mid_model_never_reaches_upstream_before_approval(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    mock_upstreams.get(TELEGRAM_UPDATES).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "anthropic/claude-3.5-haiku", "messages": [{"role": "user", "content": "reason"}]},
                             headers={"X-Run-Id": "r_fb_mid", "X-Agent-Id": "agent_fb_mid"})
    # mid tier -> approval requested -> denied on timeout, upstream never called
    assert resp.status_code == 429
    assert or_route.call_count == 0
    assert ds_route.call_count == 0

    # the DeepSeek-failure fallback's fixed target must always be cheap tier,
    # independent of which mid-tier model was originally requested
    from app.upstreams import FALLBACK_MODEL
    assert registry.get(FALLBACK_MODEL).tier == "cheap"


@pytest.mark.asyncio
async def test_fallback_alert_after_threshold(mock_upstreams):
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503))
    mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    sent = mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True}))

    for i in range(settings.fallback_alert_threshold_per_day + 2):
        async with await _client() as c:
            await c.post("/v1/chat/completions",
                          json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": f"m{i}"}]},
                          headers={"X-Run-Id": f"r_alert{i}", "X-Agent-Id": "agent_alert"})
    assert sent.call_count >= 1


@pytest.mark.asyncio
async def test_spread_rejected_pre_dispatch_when_budget_insufficient(mock_upstreams):
    # leave less headroom than the ~$0.017 a depth-4 spread costs at the
    # estimator's token assumptions, so this exercises the pre-dispatch
    # rejection rather than the token-estimate arithmetic
    ledger.record_call(
        "seed_spread", "agent_spread", "deepseek/deepseek-v4-flash", "deepseek", "cheap",
        settings.global_daily_hard - 0.01, "hspread",
    )
    async with await _client() as c:
        resp = await c.post("/v1/spread", json={"mode": "p", "depth": 4, "task": "do the thing"},
                             headers={"X-Run-Id": "r_spread1", "X-Agent-Id": "agent_spread"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "spread_budget_rejected"
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_spread_over_depth_rejected(mock_upstreams):
    async with await _client() as c:
        resp = await c.post("/v1/spread", json={"mode": "p", "depth": 5, "task": "x"},
                             headers={"X-Run-Id": "r_spread_depth", "X-Agent-Id": "agent_spread"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_spread_with_one_mid_member_asks_single_itemized_approval(mock_upstreams):
    sent = mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    mock_upstreams.get(TELEGRAM_UPDATES).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    async with await _client() as c:
        resp = await c.post("/v1/spread", json={"mode": "p", "depth": 4, "task": "x"},
                             headers={"X-Run-Id": "r_spread_mid", "X-Agent-Id": "agent_spread_mid"})
    assert resp.status_code == 429  # timeout -> deny whole spread
    assert sent.call_count == 1  # ONE itemized approval, not one per slot
    assert "slot" in sent.calls[0].request.content.decode()


def test_ledger_sums_across_both_upstreams_for_global_cap():
    ledger.record_call("r_mix1", "agent_mix_a", "deepseek/deepseek-v4-flash", "deepseek", "cheap", 1.20, "m1")
    ledger.record_call("r_mix2", "agent_mix_b", "google/gemini-2.5-flash", "openrouter", "cheap", 0.75, "m2")
    assert ledger.global_daily_spend() >= 1.95
    d = ledger.check_budgets("agent_mix_c", "r_mix3", "cheap", 0.10, "m3")
    assert d.allowed is False
    assert d.error_code == "global_daily_hard"


# --- log_only port (isaura, 8788) behavior ---

@pytest.mark.asyncio
async def test_log_only_port_allows_through_a_budget_that_would_block(mock_upstreams):
    # seed the global cap via a DIFFERENT agent so isaura's own per-agent
    # hourly cap (a cheaper/earlier check) doesn't fire first and mask what
    # this test actually targets
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    ledger.record_call("seed_logonly_hard", "some_other_agent", "deepseek/deepseek-v4-flash", "deepseek",
                        "cheap", settings.global_daily_hard + 1.0, "seedhash")
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "log-only budget test"}]})
    assert resp.status_code == 200  # would have hit global_daily_hard, but log_only lets it through
    summary = ledger.would_block_summary("isaura", time.time() - 60)
    assert summary.get("global_daily_hard", 0) >= 1


@pytest.mark.asyncio
async def test_log_only_port_still_hard_blocks_deny_list(mock_upstreams):
    # deny-list is categorical — never relaxed, even on a log_only port
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions", json={"model": "openrouter/auto", "messages": []})
    assert resp.status_code == 403
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_log_only_port_still_hard_blocks_ui_only_tier(mock_upstreams):
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "google/gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 403
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_log_only_port_mid_tier_proceeds_without_telegram_wait(mock_upstreams):
    # no TELEGRAM_SEND mock registered at all -> if the gate tried to call it,
    # respx would raise AllMockedAssertionError. Getting a clean 200 back
    # proves log_only skips the approval round-trip entirely.
    mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "anthropic/claude-3.5-haiku",
                                   "messages": [{"role": "user", "content": "log-only mid tier test"}]})
    assert resp.status_code == 200
    summary = ledger.would_block_summary("isaura", time.time() - 60)
    assert summary.get("mid_tier_would_require_approval", 0) >= 1


# --- bare DeepSeek model name compatibility (openclaw sends unprefixed ids) ---

@pytest.mark.asyncio
async def test_bare_deepseek_model_name_resolves_and_routes(mock_upstreams):
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "bare name"}]},
                             headers=HEADERS)
    assert resp.status_code == 200
    assert ds_route.call_count == 1
    sent_model = json.loads(ds_route.calls[0].request.content)["model"]
    assert sent_model == "deepseek-v4-flash"  # round-trips back to the real API's bare name


@pytest.mark.asyncio
async def test_bare_deepseek_reasoner_maps_to_real_v4_pro(mock_upstreams):
    # "deepseek-reasoner" is DeepSeek's real-world API model name, but a
    # live replay against api.deepseek.com in this environment confirmed it
    # doesn't exist here — only deepseek-v4-flash/deepseek-v4-pro are real.
    # Must resolve to the closest real equivalent (v4-pro, still cheap tier)
    # rather than 403 as unrecognized or silently 400 against the real API.
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek-reasoner", "messages": [{"role": "user", "content": "reason"}]},
                             headers=HEADERS)
    assert resp.status_code == 200
    assert ds_route.call_count == 1
    sent_model = json.loads(ds_route.calls[0].request.content)["model"]
    assert sent_model == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_deepseek_empty_body_on_2xx_falls_back_not_crashes(mock_upstreams):
    # observed live: DeepSeek occasionally returns 200 with an empty body
    # under rapid retries + heavy prompt-cache hits. Must be treated as a
    # transient failure (one fallback hop to cheap OpenRouter), never an
    # unhandled 500 from a JSONDecodeError.
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, content=b""))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                             headers={"X-Run-Id": "r_empty_body", "X-Agent-Id": "agent_empty_body"})
    assert resp.status_code == 200
    assert or_route.call_count == 1
    calls, _ = ledger.run_id_stats("r_empty_body")
    assert calls == 1


@pytest.mark.asyncio
async def test_list_content_messages_do_not_crash(mock_upstreams):
    # observed live: dcode (LangChain-based) sends message content as a list
    # of content blocks even for plain text, not a bare string. Hit a real
    # crash in ledger.prompt_hash (AttributeError: 'list' object has no
    # attribute 'strip') before this was handled.
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "deepseek/deepseek-v4-flash",
                  "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
            headers={"X-Run-Id": "r_list_content", "X-Agent-Id": "agent_list_content"},
        )
    assert resp.status_code == 200


def test_prompt_hash_handles_list_content():
    # direct unit check, independent of the HTTP path above
    h1 = ledger.prompt_hash("m", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    h2 = ledger.prompt_hash("m", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    h3 = ledger.prompt_hash("m", [{"role": "user", "content": [{"type": "text", "text": "bye"}]}])
    assert h1 == h2
    assert h1 != h3


def test_trim_old_tool_results_handles_list_content():
    from app.cache import trim_old_tool_results
    messages = [{"role": "tool", "content": [{"type": "text", "text": f"result {i}"}]} for i in range(5)]
    trimmed = trim_old_tool_results(messages, keep_recent_turns=3)
    assert "[trimmed tool result:" in trimmed[0]["content"]
    assert trimmed[-1]["content"] == messages[-1]["content"]


# --- streaming (dcode/LangChain always sends stream:true) ---

@pytest.mark.asyncio
async def test_stream_true_forces_upstream_nonstream_returns_synthetic_sse(mock_upstreams):
    # observed live: dcode (LangChain agent middleware) always sends
    # stream:true. DeepSeek/OpenRouter then legitimately return a real SSE
    # chunked stream, which broke every JSON-parsing assumption in the gate.
    # Fix: force stream:false upstream always, synthesize SSE back to the
    # client only if it asked for one.
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body(model="deepseek-v4-flash")))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert b"data: " in resp.content
    assert b"[DONE]" in resp.content

    sent_body = json.loads(ds_route.calls[0].request.content)
    assert sent_body["stream"] is False  # never actually forwarded upstream


@pytest.mark.asyncio
async def test_stream_options_stripped_before_upstream(mock_upstreams):
    # observed live: hermes's real wire request (built after its own debug
    # dump, so never visible there) sends stream:true AND
    # stream_options:{"include_usage": true}. The gate forces stream:false
    # but was still forwarding stream_options along with it — an invalid
    # combination on OpenAI-wire APIs (stream_options only valid when
    # stream:true) that DeepSeek rejected with a 400.
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body(model="deepseek-v4-flash")))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "stream": True,
                                   "stream_options": {"include_usage": True},
                                   "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 200

    sent_body = json.loads(ds_route.calls[0].request.content)
    assert sent_body["stream"] is False
    assert "stream_options" not in sent_body


@pytest.mark.asyncio
async def test_stream_true_request_still_gets_billed(mock_upstreams):
    mock_upstreams.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=_completion_body(prompt_tokens=200, completion_tokens=80))
    )
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "stream": True,
                                   "messages": [{"role": "user", "content": "bill me"}]},
                             headers={"X-Run-Id": "r_stream_bill", "X-Agent-Id": "agent_stream_bill"})
    assert resp.status_code == 200
    calls, cost = ledger.run_id_stats("r_stream_bill")
    assert calls == 1
    assert cost > 0


# --- free tier (SONNET.md section 1) ---

FREE_MODEL = "google/gemini-2.5-flash:free"


@pytest.fixture(autouse=False)
def free_model_seeded():
    # boot-time discovery normally adds this via a live OpenRouter refresh
    # (see models_registry.refresh_from_openrouter); tests don't run that
    # loop, so seed the same entry directly for the duration of the test
    from app.models_registry import ModelInfo
    registry.models[FREE_MODEL] = ModelInfo(FREE_MODEL, 0.0, 0.0, "openrouter")
    yield
    registry.models.pop(FREE_MODEL, None)


@pytest.mark.asyncio
async def test_free_model_with_qualifying_task_class_succeeds(mock_upstreams, free_model_seeded):
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers={**HEADERS, "X-Task-Class": "experiment"})
    assert resp.status_code == 200
    assert or_route.call_count == 1


@pytest.mark.asyncio
async def test_free_model_without_task_class_403s(mock_upstreams, free_model_seeded):
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "free_tier_class_required"
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_free_model_qualifies_via_port_default_task_class(mock_upstreams, free_model_seeded):
    # opencode (8792) has default_task_class: experiment in ports.yaml — no
    # header needed
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client(8792) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert or_route.call_count == 1


@pytest.mark.asyncio
async def test_isaura_never_qualifies_for_free_tier_even_with_header(mock_upstreams, free_model_seeded):
    # hardcoded exclusion — no exceptions list, even a qualifying header on
    # isaura's own port must not grant free-tier access
    async with await _client(8788) as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers={"X-Task-Class": "experiment"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "free_tier_class_required"
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_free_tier_429_escalates_to_next_live_ladder_rung(mock_upstreams, free_model_seeded):
    # ROUTING_RESILIENCE §3: a rate-limited free rung escalates down the same
    # provider-escalation ladder as any other failure (deepseek -> aistudio ->
    # groq -> openrouter:free -> openrouter:cheap) rather than retrying a
    # same-provider paid sibling — in the test env aistudio/groq are
    # unconfigured, so deepseek (rung 1) is the next live rung after
    # openrouter's :free primary attempt (rung 0) 429s.
    free_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(429))
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers={**HEADERS, "X-Task-Class": "experiment"})
    assert resp.status_code == 200
    assert free_route.call_count == 1
    assert ds_route.call_count == 1
    assert ledger.free_fallback_count_today() >= 1


@pytest.mark.asyncio
async def test_free_calls_still_trip_per_run_cap(mock_upstreams, free_model_seeded):
    for i in range(100):
        ledger.record_call("r_free_loop", "agent_free", FREE_MODEL, "openrouter", "free", 0.0, f"free{i}")
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FREE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers={"X-Run-Id": "r_free_loop", "X-Agent-Id": "agent_free",
                                      "X-Task-Class": "experiment"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "run_budget_killed"


# --- surge mode (SONNET.md section 2) ---

FRONTIER_MODEL = "google/gemini-2.5-pro"  # blend 3.4375, ui_only


@pytest.mark.asyncio
async def test_frontier_403_without_surge(mock_upstreams):
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": FRONTIER_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                             headers=HEADERS)
    assert resp.status_code == 403
    assert not mock_upstreams.calls


@pytest.mark.asyncio
async def test_frontier_approval_during_allow_frontier_surge(mock_upstreams):
    sent = mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    mock_upstreams.get(TELEGRAM_UPDATES).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    decision = ledger.start_surge(minutes=10, extra_cap=5.0, allow_frontier=True, reason="test surge")
    assert decision.allowed
    try:
        async with await _client() as c:
            resp = await c.post("/v1/chat/completions",
                                 json={"model": FRONTIER_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                                 headers={"X-Run-Id": "r_frontier", "X-Agent-Id": "agent_frontier"})
        # per-call itemized approval, same mechanism as mid-tier -> denies on
        # timeout, never a silent 403 and never a silent allow
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "approval_timeout"
        assert sent.call_count == 1
    finally:
        ledger.end_surge()


def test_surge_without_allow_frontier_still_403s():
    decision = ledger.start_surge(minutes=10, extra_cap=5.0, allow_frontier=False, reason="cap only, no frontier")
    assert decision.allowed
    try:
        assert ledger.frontier_approval_allowed() is False
    finally:
        ledger.end_surge()


def test_surge_cap_math_base_plus_extra():
    base = settings.global_daily_hard
    decision = ledger.start_surge(minutes=10, extra_cap=5.0, allow_frontier=False, reason="cap math test")
    assert decision.allowed
    try:
        assert ledger.effective_global_daily_hard() == base + 5.0
        assert ledger.effective_mid_tier_daily_cap() == settings.mid_tier_daily_cap * 2
    finally:
        ledger.end_surge()


def test_surge_auto_expiry_restores_base_caps():
    now = time.time()
    decision = ledger.start_surge(minutes=1, extra_cap=5.0, allow_frontier=False, reason="short surge")
    assert decision.allowed
    # still active right now
    assert ledger.effective_global_daily_hard(now) == settings.global_daily_hard + 5.0
    # 2 minutes later, TTL has elapsed -> reverts to base without any manual end
    later = now + 120
    assert ledger.active_surge(later) is None
    assert ledger.effective_global_daily_hard(later) == settings.global_daily_hard


def test_4th_surge_in_7_days_refused():
    for i in range(3):
        decision = ledger.start_surge(minutes=1, extra_cap=1.0, allow_frontier=False, reason=f"surge {i}")
        assert decision.allowed, f"surge {i} should have been allowed"
        ledger.end_surge()
    fourth = ledger.start_surge(minutes=1, extra_cap=1.0, allow_frontier=False, reason="surge 4")
    assert fourth.allowed is False
    assert "3 surges per 7 days" in fourth.reason
    assert "surge 0" in fourth.reason and "surge 1" in fourth.reason and "surge 2" in fourth.reason


def test_surge_requires_reason():
    decision = ledger.start_surge(minutes=10, extra_cap=1.0, allow_frontier=False, reason="")
    assert decision.allowed is False
    assert "reason" in decision.reason.lower()


def test_only_one_active_surge_at_a_time():
    first = ledger.start_surge(minutes=10, extra_cap=1.0, allow_frontier=False, reason="first")
    assert first.allowed
    try:
        second = ledger.start_surge(minutes=10, extra_cap=1.0, allow_frontier=False, reason="second")
        assert second.allowed is False
        assert "already active" in second.reason
    finally:
        ledger.end_surge()


# --- ROUTING_RESILIENCE.md §5 (the remaining 5 of 6 named tests; #4 "7
# attempts in one chain -> 429 loop_detected" is already covered above by
# test_repeat_prompt_hash_loop_detected / test_synthesized_run_id_still_enforces_loop_detection) ---

def test_empty_completion_twice_marks_provider_unfunded_not_retried():
    # §5 test 1: a 200 with zero content tokens is the unfunded signature. One
    # is suspicious but not conclusive (stays usable); two in a row marks the
    # provider unfunded, so dispatch stops retrying it (app/health.py).
    from app import health
    empty_body = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
    health.record_result("deepseek", 200, empty_body)
    assert health.get("deepseek").status == health.UNKNOWN
    assert health.is_usable("deepseek")
    health.record_result("deepseek", 200, empty_body)
    assert health.get("deepseek").status == health.UNFUNDED
    assert not health.is_usable("deepseek")


@pytest.mark.asyncio
async def test_rung1_unfunded_dispatch_starts_at_rung2_zero_calls_to_rung1(mock_upstreams):
    # §5 test 2: the preflight health cache already knows rung 1 is dead
    # (e.g. from the probe loop) -> dispatch skips it before ever calling it,
    # landing on the next live rung (rungs 2/3 unconfigured in the test env,
    # so rung 4 openrouter:free is the first live hop).
    from app import health
    health.mark("deepseek", health.UNFUNDED, "200 with 0 content tokens (2x)")
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                             headers={"X-Run-Id": "r_rung1dead", "X-Agent-Id": "agent_rung1dead"})
    assert resp.status_code == 200
    assert ds_route.call_count == 0
    assert or_route.call_count == 1


@pytest.mark.asyncio
async def test_hermes_shaped_retries_plus_failover_share_one_chain_no_429(mock_upstreams):
    # §5 test 3: Hermes's own 3 retries + 1 failover for the SAME logical
    # request (identical run_id + normalized body) must land in one attempt
    # chain, not four, so it never trips loop detection meant for a genuine
    # runaway (ROUTING_RESILIENCE §2). temperature>0.3 makes this
    # non-cacheable, so all 4 calls actually reach the ledger instead of the
    # first one satisfying the rest from cache (see
    # test_synthesized_run_id_still_enforces_loop_detection above).
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))
    payload = {"model": "deepseek/deepseek-v4-flash", "temperature": 0.9,
               "messages": [{"role": "user", "content": "chained"}]}
    headers = {"X-Run-Id": "r_chain1", "X-Agent-Id": "agent_chain1"}
    for _ in range(4):  # 3 retries + 1 failover, Hermes-shaped
        async with await _client() as c:
            resp = await c.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp.status_code == 200

    p_hash = ledger.prompt_hash(payload["model"], payload["messages"])
    now = time.time()
    assert ledger.chain_attempt_count(p_hash, now) == 4
    # anchor both timestamps just inside the same chain window, away from its
    # boundary, so this isn't flaky the ~1-in-120 times `now` lands near an edge
    bucket_start = (now // settings.chain_window_seconds) * settings.chain_window_seconds
    safe_now = bucket_start + 1
    assert ledger.chain_id("r_chain1", p_hash, safe_now) == ledger.chain_id("r_chain1", p_hash, safe_now + 1)


@pytest.mark.asyncio
async def test_all_rungs_dead_returns_503_envelope_with_every_rung_and_next_action(mock_upstreams):
    # §5 test 5: when every rung is exhausted, the 503 envelope must account
    # for every rung in the ladder (attempted or skipped) and name a single
    # next action. aistudio/groq are unconfigured in the test env; deepseek
    # and openrouter both fail live.
    ds_route = mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(return_value=httpx.Response(503))
    mock_upstreams.post(TELEGRAM_SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions",
                             json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "a"}]},
                             headers={"X-Run-Id": "r_dead", "X-Agent-Id": "agent_dead"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "no_live_route"
    # deepseek/openrouter each only actually hit once, not once per rung that
    # shares their provider (routing.yaml rungs 1/4 collapse to 2 accounts)
    assert ds_route.call_count == 1
    assert or_route.call_count == 1
    rungs_seen = {row["rung"] for row in body["attempted"] + body["skipped_by_health_cache"]}
    assert {1, 2, 3, 4} <= rungs_seen
    assert body["next_action"].startswith("Fix rung")
    assert body["health_snapshot"] == "/admin/health"


def test_ladder_never_contains_mid_or_frontier_tier():
    # §5 test 6: escalation must never raise tier (ROUTING_RESILIENCE §3) — a
    # free-rung failure can only fall to another free or cheap rung. This is
    # a property of routing.yaml itself (dispatch has no other tier limiter),
    # so guard it directly against a future rung being added above cheap.
    from app.routing import ladder
    from app.models_registry import FREE, CHEAP
    for client_facing in (False, True):
        rungs = ladder(client_facing)
        assert rungs, "ladder must not be empty"
        assert all(r.tier in (FREE, CHEAP) for r in rungs)


@pytest.mark.asyncio
async def test_admin_health_endpoint_returns_upstream_snapshot(mock_upstreams):
    # regression: app/routes/admin.py did `from app import health`, but the
    # pre-existing `@router.get("/health") async def health():` on the same
    # module rebinds the module-level name `health` to that function —
    # shadowing the imported module, so `health.snapshot()` 500'd with
    # AttributeError in production the moment this route was actually hit.
    async with await _client() as c:
        resp = await c.get("/admin/health")
    assert resp.status_code == 200
    body = resp.json()
    # groq dropped from the ladder 2026-09-28 (I7_MAIN.md §2.5): confirmed
    # live that llama-3.3-70b-versatile is no longer in Groq's free tier.
    assert set(body["upstreams"]) == {"deepseek", "aistudio", "openrouter"}


@pytest.mark.asyncio
async def test_synthesized_sse_stream_preserves_tool_calls():
    # regression (2026-07-29): app/sse.py rebuilt the delta as {role, content}
    # only, so a tool-calling turn — content empty, everything in tool_calls —
    # reached the client as an empty assistant message with finish_reason
    # "tool_calls" and no tool_calls payload. Every Hermes cron agent sends
    # stream:true + tools, so each turn looked like an "empty response": the
    # agent retried the byte-identical body 3x, failed over, retried 3x more,
    # and the 7th attempt tripped the gate's own chain loop detector (429
    # loop_detected). Cron jobs failed while no_agent script jobs kept working.
    from app.sse import synthesize_sse_stream

    tool_call = {
        "id": "call_abc123",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    }
    body = {
        "id": "cmpl-1", "created": 1, "model": "deepseek-v4-flash",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
                "reasoning_content": "The user wants weather, so call the tool.",
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"total_tokens": 42},
    }

    chunks = [
        json.loads(raw.decode().removeprefix("data: ").strip())
        async for raw in synthesize_sse_stream(body)
        if not raw.startswith(b"data: [DONE]")
    ]

    delta = chunks[0]["choices"][0]["delta"]
    assert delta["tool_calls"] == [{**tool_call, "index": 0}]
    assert delta["reasoning_content"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# ESCALATION_POLICY.md §6 — two ladders, split by trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ladderA_500_retries_same_model_on_next_provider_blend_unchanged(mock_upstreams):
    # §6 test 1: availability failure keeps the MODEL constant and changes only
    # the PROVIDER, so the blend a caller pays cannot rise because an upstream
    # broke. Rung 1 (deepseek) 500s; rung 2 must be the SAME model on openrouter.
    from app.routing import ladder

    rungs = ladder()
    assert rungs[0].model == rungs[1].model, "rung 1->2 must not change model"
    assert rungs[0].provider != rungs[1].provider, "rung 1->2 must change provider"
    assert registry.get(rungs[0].model).blend == registry.get(rungs[1].model).blend

    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(500))
    or_route = mock_upstreams.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=_completion_body())
    )
    async with await _client() as c:
        resp = await c.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "deepseek/deepseek-v4-flash",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert resp.status_code == 200
    assert or_route.called
    sent = json.loads(or_route.calls[0].request.content)
    assert sent["model"] == "deepseek/deepseek-v4-flash", "failover must not swap the model"


def test_ladderA_outage_never_crosses_into_ladderB():
    # §6 test 2: an availability outage must never become a quality escalation.
    # Every Ladder-A rung must cost no more than the rung it fails over FROM,
    # so no provider outage can raise the price of a request.
    from app.routing import ladder

    blends = []
    for r in ladder():
        if r.free:
            blends.append(0.0)          # free-tier rung costs nothing per token
            continue
        info = registry.get(r.model)
        # A paid rung whose price we cannot resolve cannot be PROVEN cheaper,
        # so it counts as infinitely expensive and fails this invariant loudly
        # rather than passing by accident.
        blends.append(info.blend if info else float("inf"))

    assert blends == sorted(blends, reverse=True), (
        f"Ladder A must never ascend in price (non-increasing required): {blends}"
    )
    assert max(blends) <= blends[0], "no Ladder-A rung may cost more than rung 1"


def test_self_assessed_low_confidence_never_escalates():
    # §6 test 3: hardcoded refusal. A model grading its own answer has no
    # ceiling and no auditor — functionally openrouter/auto.
    from app import escalation
    from app.ledger import escalation_count_today

    before = escalation_count_today()
    for trigger in ("low_confidence", "self_assessed", "quality", "retry", "vibes"):
        d = escalation.evaluate("agentQ", trigger, "deepseek/deepseek-v4-flash", hops_used=0)
        assert d.allowed is False, f"{trigger!r} must not escalate"
        assert d.code == "escalation_refused"
    assert escalation_count_today() == before, "a refused trigger must not consume budget"


def test_malformed_json_escalates_but_never_above_the_ceiling():
    # §6 test 4: a deterministic trigger DOES escalate, and every automatic hop
    # stays at or under auto_ceiling_blend. Note: with DeepSeek's confirmed
    # prices ds-pro blends $0.544 and is therefore a legal automatic rung —
    # the policy is "follow the arithmetic", so the invariant under test is the
    # ceiling itself, not any single model's name.
    from app import escalation
    from app.routing import auto_ceiling_blend, quality_ladder

    d = escalation.evaluate("agentQ", escalation.SCHEMA_PARSE_FAILED,
                            "deepseek/deepseek-v4-flash", hops_used=0)
    assert d.allowed is True
    assert d.target_model is not None
    assert registry.get(d.target_model).blend <= auto_ceiling_blend()
    assert all(r.blend <= auto_ceiling_blend() for r in quality_ladder())

    # and it stops after max_auto_hops rather than climbing forever
    exhausted = escalation.evaluate("agentQ", escalation.TOOL_CALL_MALFORMED,
                                    "deepseek/deepseek-v4-pro", hops_used=2)
    assert exhausted.allowed is False


def test_16th_escalation_in_a_day_returns_escalation_budget():
    # §6 test 5: escalation FREQUENCY is its own budget, separate from dollars.
    from app import escalation
    from app.ledger import record_escalation

    for i in range(settings.escalation_daily_max):
        record_escalation("agentQ", f"c_{i}", escalation.SCHEMA_PARSE_FAILED,
                          "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro")
    d = escalation.evaluate("agentQ", escalation.SCHEMA_PARSE_FAILED,
                            "deepseek/deepseek-v4-flash", hops_used=0)
    assert d.allowed is False
    assert d.status == 429
    assert d.code == "escalation_budget"


def test_verify_script_rejects_a_client_side_fallback_chain(tmp_path):
    # §4 companion test: the guard must FAIL when any agent config declares a
    # fallback chain. The gate cannot see a client-side fallback — the agent
    # resolves openrouter/auto to a concrete model before the request arrives,
    # so the deny list never fires.
    import subprocess

    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_migration.sh"
    assert script.exists()
    body = script.read_text()
    assert "fallbacks" in body, "verify_migration.sh must check for fallback chains"

    offending = tmp_path / "openclaw.json"
    offending.write_text(json.dumps(
        {"agents": {"defaults": {"model": {"primary": "m", "fallbacks": ["openrouter/auto"]}}}}
    ))
    probe = subprocess.run(
        ["python3", "-c",
         "import json,sys\n"
         "d=json.load(open(sys.argv[1]))\n"
         "m=d.get('agents',{}).get('defaults',{}).get('model',{})\n"
         "print('HIT' if m.get('fallbacks') else '')",
         str(offending)],
        capture_output=True, text=True,
    )
    assert "HIT" in probe.stdout, "the fallback detector must flag this config"


# ---------------------------------------------------------------------------
# LADDER_V2.md — index floor 45, GLM-4.7 Flash on Ladder A, Qwen removed
# ---------------------------------------------------------------------------


def test_boot_rejects_a_rung_below_the_index_floor(tmp_path, monkeypatch):
    # LADDER_V2 §1: models below the floor are excluded from config entirely,
    # not merely deprioritised — so this is a hard boot failure.
    import yaml as _yaml
    from app import routing

    cfg = _yaml.safe_load(pathlib.Path(routing.ROUTING_YAML_PATH).read_text())
    cfg["min_index"] = 45
    cfg["ladder"][0]["index"] = 38           # MiMo V2 Pro territory
    bad = tmp_path / "routing.yaml"
    bad.write_text(_yaml.safe_dump(cfg))

    monkeypatch.setattr(routing, "ROUTING_YAML_PATH", bad)
    with pytest.raises(routing.IndexFloorError):
        routing.reload()
    monkeypatch.undo()
    routing.reload()                          # restore real ladder for other tests


def test_free_rungs_are_exempt_from_the_index_floor():
    # §2.1: free-tier rungs are floor-exempt because $0 changes the calculus.
    from app.routing import ladder

    free = [r for r in ladder() if r.free]
    assert free, "ladder should still carry free-tier rungs"
    # they sit below the floor's spirit but must load without raising
    assert all(r.index >= 0 for r in free)


def test_ladderB_requires_strictly_increasing_index():
    # §2: anything at or below the current index is lateral, never a quality
    # escalation target.
    from app import escalation
    from app.routing import quality_ladder

    indices = [r.index for r in quality_ladder()]
    assert indices == sorted(indices)

    # flash (50) must escalate to pro (52), never sideways to flash elsewhere
    target = escalation.next_rung("deepseek/deepseek-v4-flash", hops_used=0)
    assert target is not None
    assert target[0] == "deepseek/deepseek-v4-pro"

    # from the top rung there is nowhere higher to go
    assert escalation.next_rung("deepseek/deepseek-v4-pro", hops_used=0) is None


def test_glm47_flash_is_a_ladderA_rung_and_does_not_raise_cost():
    # The operator's table averaged (in+out)/2, which makes GLM-4.7 Flash look
    # more expensive than DS Flash ($0.23 vs $0.21). The gate weights 0.75/0.25,
    # and GLM's cheap input ($0.06) makes it CHEAPER on that basis — so it is a
    # legitimate non-increasing failover hop. Guard the real invariant.
    from app.routing import ladder

    rungs = ladder()
    glm = [r for r in rungs if "glm-4.7" in r.model]
    assert glm, "GLM-4.7 Flash must be on Ladder A"
    assert glm[0].provider == "openrouter"

    ds_blend = registry.get("deepseek/deepseek-v4-flash").blend
    glm_blend = registry.get(glm[0].model).blend
    assert glm_blend <= ds_blend, f"GLM-4.7 Flash ${glm_blend} must not exceed DS Flash ${ds_blend}"


def test_qwen_is_absent_from_every_ladder():
    # Operator instruction 2026-08-04: Qwen 3.5 397B out. It was LADDER_V2's
    # terminal third-vendor outage rung, so with it gone a DeepSeek+Z.ai double
    # outage falls to free-tier and then 503 — asserted here so its removal is
    # deliberate and visible rather than silent.
    from app.routing import above_ceiling, ladder, quality_ladder

    everything = list(ladder()) + list(quality_ladder()) + list(above_ceiling())
    assert not any("qwen" in r.model.lower() for r in everything)
