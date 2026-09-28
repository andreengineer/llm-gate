from __future__ import annotations

import logging
import os
from typing import NoReturn

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cache import (
    cache_key, get as cache_get, put as cache_put, is_cacheable,
    reject_if_duplicate_blocks, trim_old_tool_results,
)
from app.config import settings
from app.deny_list import check_fallback_array, deny_reason
from app.estimate import estimate_cost
from app import health, routing
from app.ledger import (
    chain_id, check_budgets, fallback_hop_count_today, free_fallback_count_today,
    frontier_approval_allowed, prompt_hash, record_call, record_rejection,
    record_would_block,
)
from app.models_registry import CHEAP, FREE, MID, UI_ONLY, registry, resolve_model_id
from app.sse import synthesize_sse_stream
from app.telegram import alert_no_live_route, request_mid_tier_approval, send_alert
from app.upstreams import (
    NoLiveRouteError, UpstreamBadResponseError, compute_cost, dispatch as upstream_dispatch,
)

logger = logging.getLogger("llm-gate.chat")
router = APIRouter()

UI_ONLY_DIRECTIVE = (
    "model {model} blend ${blend:.2f}/M exceeds API ceiling (${ceiling:.2f}).\n"
    "Route per ROUTING_RULES: strategic/irreversible -> claude.ai (sunk),\n"
    "architecture/code -> ChatGPT or Claude Code (sunk), live web -> Perplexity Pro UI (sunk)."
)

# Free tier is for low-stakes background/experimental workloads only.
# Hardcoded, not config-driven — no exceptions list, ever: client-facing
# traffic doesn't get the unmetered tier no matter what header it sends.
NEVER_FREE_TIER_AGENTS = {"isaura"}

# Client-facing paths that must never silently degrade onto a free experimental
# rung by failover (ROUTING_RESILIENCE §3 task-class gate). Same set as the
# never-free agents: if every client_ok rung is dead, they get the §4 envelope,
# not a quietly downgraded answer.
CLIENT_FACING_AGENTS = NEVER_FREE_TIER_AGENTS

# Free-tier providers, for ranking the cheapest fix in the failure envelope: a
# key regen on a free provider needs no payment, so it's the cheapest next step.
_FREE_PROVIDERS = {"aistudio", "groq"}


def _rung_action(result: str) -> str:
    # "unconfigured" isn't an upstream result code — it maps to the same fix as
    # a dead key: set/replace the key in ~/llm-gate/.env.
    return routing.remediation("unauthorized" if result == "unconfigured" else result)


def _next_action(items: list[dict]) -> str:
    """Name the single cheapest fix (§4): a free-provider key regen beats a paid
    key regen beats waiting on a rate limit beats waiting on banking."""
    if not items:
        return "no rungs configured — add a provider key to ~/llm-gate/.env"

    def rank(it: dict) -> int:
        result, provider = it["result"], it["provider"]
        if result in ("unauthorized", "unconfigured") and provider in _FREE_PROVIDERS:
            return 0
        if result in ("unauthorized", "unconfigured"):
            return 1
        if result == "rate_limited":
            return 2
        if result == "unfunded":
            return 3
        return 4

    best = min(items, key=lambda it: (rank(it), it["rung"]))
    fix = _rung_action(best["result"])
    return f"Fix rung {best['rung']} ({best['provider']}) — {fix}"


def _no_live_route_envelope(cid: str, attempted: list, skipped: list[dict]) -> dict:
    attempted_rows = [
        {"rung": a.rung, "provider": a.provider, "model": a.model,
         "result": a.result, "detail": a.detail, "action": _rung_action(a.result)}
        for a in attempted
    ]
    skipped_rows = [
        {**s, "action": _rung_action(s["result"])} for s in skipped
    ]
    return {
        "error": "no_live_route",
        "chain_id": cid,
        "attempted": attempted_rows,
        "skipped_by_health_cache": skipped_rows,
        "next_action": _next_action(attempted_rows + skipped_rows),
        "health_snapshot": "/admin/health",
    }


def _tunable_block(log_only: bool, run_id: str, agent_id: str, status_code: int,
                   error_code: str, reason: str, model: str = "") -> None:
    """Deny-list and the ui-only tier ceiling are hard, categorical rules and
    NEVER go through here — blocking them costs nothing in lost
    observability. This is only for tunable numeric/threshold checks
    (budgets, loop detection, dedup, mid-tier approval) that a log_only port
    should observe-and-allow instead of enforce, so a false-positive
    threshold can't break a newly-repointed agent on day one.

    ENFORCED refusals are RECORDED before raising (record_rejection). An
    enforced "no" used to write nothing anywhere — no calls row (the request
    never reached record_call) and no would_block_events row (log_only only) —
    so 11 days of dedup_rejected 400s were invisible and the only symptom was
    silent agents.

    Emergency escape hatch: DEDUP_GUARD_LOG_ONLY=1 downgrades dedup_rejected to
    observe-and-allow even on an enforce port. Every production agent port is
    enforce, so without this a single bad threshold takes down scheduled jobs
    (ES 2026-09-16); the escape hatch is narrow (dedup only) and env-scoped so
    it needs no redeploy of the port map.
    """
    if error_code == "dedup_rejected" and os.environ.get("DEDUP_GUARD_LOG_ONLY") == "1":
        log_only = True
    if log_only:
        record_would_block(run_id, agent_id, error_code, reason)
        logger.warning("[LOG-ONLY %s] would block (%s): %s", agent_id, error_code, reason)
        return
    record_rejection(run_id, agent_id, error_code, status_code,
                     reason=reason, enforcement="enforce", model=model)
    logger.warning("[ENFORCED %s] blocked (%s): %s", agent_id, error_code, reason)
    raise HTTPException(status_code, detail={"error": error_code, "reason": reason})


def _hard_block(run_id: str, agent_id: str, status_code: int, error_code: str,
                reason, model: str = "", detail=None) -> NoReturn:
    """Hard, categorical refusal: deny-list, fallback array, dropped/unknown
    model, ui-only frontier ceiling, free-tier class, approval timeout.

    Never relaxed by log_only — these are not numeric thresholds. Records the
    refusal as enforcement="hard" immediately before raising so a blocked
    agent always leaves a trace. `detail` preserves the site's exact existing
    HTTP response body.
    """
    if not isinstance(reason, str):
        reason = str(reason)
    record_rejection(run_id, agent_id, error_code, status_code,
                     reason=reason, enforcement="hard", model=model)
    logger.warning("[HARD-BLOCK %s] %s: %s", agent_id, error_code, reason)
    raise HTTPException(status_code,
                        detail=detail if detail is not None
                        else {"error": error_code, "reason": reason})


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    x_agent_id = request.state.agent_id
    x_run_id = request.state.run_id
    log_only = request.state.enforcement == "log_only"
    payload = await request.json()
    model = payload.get("model")
    if not model:
        raise HTTPException(400, "model is required")
    model = resolve_model_id(model)
    payload["model"] = model

    # LangChain-based agent frameworks (dcode, likely others) force
    # stream:true internally even when the end-user experience isn't
    # streaming. Every upstream call is forced non-streaming so budget/
    # cache/ledger logic stays against a single response body; a stream is
    # synthesized back to the client afterward if it asked for one.
    client_wants_stream = bool(payload.get("stream"))
    payload["stream"] = False

    # I7_MAIN.md §2.3: caller opt-in to DeepSeek thinking (default is forced
    # off in app.upstreams._forced_params). This marker never reaches any
    # upstream body — _forced_params pops it unconditionally.
    if request.headers.get("x-think") == "1":
        payload["_think_requested"] = True

    reason = deny_reason(model)
    if reason:
        _hard_block(x_run_id, x_agent_id, 403, "deny_list", reason, model,
                    detail=f"model {model} denied: {reason}")

    fb_reason = check_fallback_array(payload.get("models"), set(registry.models.keys()))
    if fb_reason:
        _hard_block(x_run_id, x_agent_id, 403, "fallback_array", fb_reason, model,
                    detail=fb_reason)

    if model in registry.dropped:
        _hard_block(x_run_id, x_agent_id, 403, "model_dropped",
                    registry.dropped[model], model,
                    detail=f"model {model} auto-dropped: {registry.dropped[model]}")

    info = registry.get(model)
    if info is None:
        _hard_block(x_run_id, x_agent_id, 403, "not_in_allowlist",
                    f"model {model} not in allowlist", model,
                    detail=f"model {model} not in allowlist")

    tier = info.tier
    if tier == UI_ONLY and not frontier_approval_allowed():
        # Standing constraint: never rewritten to a cheap model, never
        # relaxed by log_only — 403s so the caller gets fixed. The ONLY
        # sanctioned exception is an active surge with --allow-frontier,
        # handled below as a per-call approval, same as mid-tier.
        _hard_block(
            x_run_id, x_agent_id, 403, "frontier_ceiling",
            f"ui-only model {model} needs an active surge with --allow-frontier",
            model,
            detail=UI_ONLY_DIRECTIVE.format(model=model, blend=info.blend,
                                            ceiling=settings.tier_mid_max),
        )

    if tier == FREE:
        task_class = getattr(request.state, "task_class", None)
        if x_agent_id in NEVER_FREE_TIER_AGENTS or task_class is None:
            _hard_block(
                x_run_id, x_agent_id, 403, "free_tier_class_required",
                "free-tier models need a qualifying X-Task-Class "
                "(background|experiment|cron) or a port with one set; "
                "client-facing agents never qualify",
                model,
                detail={
                    "error": "free_tier_class_required",
                    "reason": "free-tier models need a qualifying X-Task-Class "
                              "(background|experiment|cron) or a port with one set; "
                              "client-facing agents never qualify",
                },
            )

    messages = payload.get("messages", [])
    # Background/cron jobs legitimately paste last run's report into a user
    # turn for comparison (Weekly Model Review, outcome-agent-weekly-recon) —
    # a real, >60%-duplicate user turn that isn't a loop. The loop risk this
    # guard exists for is interactive/live traffic; background/cron traffic
    # doesn't carry that risk, so it's exempted rather than raising the
    # threshold globally and reopening the door for live agents.
    task_class = getattr(request.state, "task_class", None)
    if task_class not in ("background", "cron"):
        dup_reason = reject_if_duplicate_blocks(messages)
        if dup_reason:
            _tunable_block(log_only, x_run_id, x_agent_id, 400, "dedup_rejected", dup_reason,
                           model=model)

    messages = trim_old_tool_results(messages)
    payload["messages"] = messages

    temperature = payload.get("temperature")
    tools = payload.get("tools")
    max_tokens = payload.get("max_tokens", settings.default_max_tokens)

    p_hash = prompt_hash(model, messages)
    cacheable = is_cacheable(temperature, tools)
    ckey = cache_key(model, messages, temperature, tools) if cacheable else None

    if cacheable:
        cached = cache_get(ckey)
        if cached is not None:
            if client_wants_stream:
                return StreamingResponse(
                    synthesize_sse_stream(cached),
                    media_type="text/event-stream",
                    headers={"X-Gate-Cache": "hit"},
                )
            return JSONResponse(content=cached, headers={"X-Gate-Cache": "hit"})

    est_cost = estimate_cost(info, messages, max_tokens)

    async with httpx.AsyncClient() as client:
        if tier in (MID, UI_ONLY):
            # reaching here with UI_ONLY already implies an active surge
            # with --allow-frontier (checked above) — same per-call
            # itemized approval mechanism as mid-tier, just a pricier model
            block_reason = "mid_tier_would_require_approval" if tier == MID else "frontier_would_require_approval"
            if log_only:
                record_would_block(x_run_id, x_agent_id, block_reason,
                                    f"model={model} est_cost=${est_cost:.4f}")
                logger.warning("[LOG-ONLY %s] %s call would require approval: %s est $%.4f",
                               x_agent_id, tier, model, est_cost)
            else:
                from app.ledger import global_daily_spend, mid_tier_daily_spend
                approved = await request_mid_tier_approval(
                    client, x_agent_id, x_run_id, model, est_cost,
                    mid_tier_daily_spend(), global_daily_spend(),
                    str(messages[-1].get("content", "")) if messages else "",
                )
                if not approved:
                    _hard_block(
                        x_run_id, x_agent_id, 429, "approval_timeout",
                        f"{tier} approval timed out for {model}", model,
                        detail={
                            "error": "approval_timeout",
                            "hint": "degrade to cheap tier once or fail task; never retry same model",
                        },
                    )

        decision = check_budgets(x_agent_id, x_run_id, tier, est_cost, p_hash)
        if not decision.allowed:
            _tunable_block(log_only, x_run_id, x_agent_id, decision.status_code,
                           decision.error_code, decision.reason, model=model)

        client_facing = x_agent_id in CLIENT_FACING_AGENTS
        try:
            result = await upstream_dispatch(client, model, payload, client_facing=client_facing)
        except NoLiveRouteError as exc:
            # Total outage: every rung skipped (known-dead) or attempted-and-failed.
            # One structured 503 that ends the debugging session in one read, plus
            # a single deduplicated Telegram (§4).
            cid = chain_id(x_run_id, p_hash)
            envelope = _no_live_route_envelope(cid, exc.attempted, exc.skipped)
            logger.error("no_live_route chain=%s next_action=%s", cid, envelope["next_action"])
            await alert_no_live_route(client, cid, envelope["next_action"])
            return JSONResponse(status_code=503, content=envelope)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                exc.response.status_code,
                f"upstream error: {exc.response.status_code} - {exc.response.text[:300]}",
            ) from exc
        except UpstreamBadResponseError as exc:
            raise HTTPException(502, f"upstream error: {exc}") from exc

        cost = compute_cost(result.model_used, result.prompt_tokens, result.completion_tokens,
                            cache_hit_tokens=result.cache_hit_tokens)
        record_call(x_run_id, x_agent_id, result.model_used, result.upstream_used, tier, cost,
                    p_hash, fallback_hop=result.fallback_hop, free_fallback=result.free_fallback)

        if result.fallback_hop:
            count = fallback_hop_count_today()
            if count > settings.fallback_alert_threshold_per_day:
                await send_alert(
                    client,
                    f"fallback_hop count today = {count} (>{settings.fallback_alert_threshold_per_day}): "
                    f"DeepSeek looks unstable, not that budget should grow",
                )

        if result.free_fallback:
            count = free_fallback_count_today()
            if count > 20:
                # informational only — means free-tier quota is exhausted for
                # the day, not a budget breach (the sibling call still costs
                # real money at cheap-tier price, already billed above)
                await send_alert(
                    client,
                    f"free_fallback count today = {count} (>20): free-tier quota looks exhausted",
                )

    if cacheable:
        cache_put(ckey, result.body)

    if client_wants_stream:
        return StreamingResponse(
            synthesize_sse_stream(result.body),
            media_type="text/event-stream",
            headers={"X-Gate-Cache": "miss"},
        )
    return JSONResponse(content=result.body, headers={"X-Gate-Cache": "miss"})
