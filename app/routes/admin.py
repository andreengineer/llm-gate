from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request

from app import health as upstream_health_cache
from app.halt import halt, is_halted, unlock
from app.ledger import (
    active_surge, end_surge, global_daily_spend, last_sentinel_event,
    mid_tier_daily_spend, recent_rejections, record_sentinel_event,
    sentinel_activity, set_outcome, start_surge, would_block_summary,
)
from app.models_registry import registry
from app.ports import all_ports, identity_for_port
from app.telegram import send_message

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "halted" if is_halted() else "ok",
        "ports": {
            str(p): {"agent_id": identity_for_port(p).agent_id, "enforcement": identity_for_port(p).enforcement}
            for p in all_ports()
        },
        "global_daily_spend": global_daily_spend(),
        "mid_tier_daily_spend": mid_tier_daily_spend(),
        "dropped_models": registry.dropped,
        "last_price_refresh": registry.last_refresh,
    }


@router.get("/admin/health")
async def upstream_health():
    """The one command to run when generation breaks (ROUTING_RESILIENCE §1):
    per-upstream cached health, whether routing will skip it, and why."""
    return {"upstreams": upstream_health_cache.snapshot()}


@router.get("/admin/would-block/{agent_id}")
async def would_block_review(agent_id: str, since_hours: float = 24.0):
    import time
    since = time.time() - since_hours * 3600
    return {"agent": agent_id, "since_hours": since_hours, "would_have_blocked": would_block_summary(agent_id, since)}


@router.get("/admin/rejections")
async def rejections(since_hours: float = 24.0):
    """Every ENFORCED refusal in the window.

    This is the query that would have caught the 11-day dedup outage on day
    one. An enforced refusal now writes a `rejections` row AND a `calls` row
    with outcome='rejected:<code>' — before the silent-canary wiring an
    enforced "no" wrote nothing at all, so the only symptom was agents going
    quiet with nothing in the ledger to explain it.
    """
    rows = recent_rejections(since_hours)
    by_code: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for _ts, agent, _sc, code, _run in rows:
        by_code[code] = by_code.get(code, 0) + 1
        by_agent[agent] = by_agent.get(agent, 0) + 1
    return {
        "since_hours": since_hours,
        "count": len(rows),
        "by_code": by_code,
        "by_agent": by_agent,
        "rows": [
            {"ts": ts, "agent": a, "status_code": sc, "reason_code": rc, "run_id": rid}
            for ts, a, sc, rc, rid in rows
        ],
    }


@router.get("/admin/silence-canary")
async def silence_canary(activity_s: float = 3600.0, success_s: float = 7200.0):
    """Outage detector: agents that made calls inside `activity_s` but have no
    SUCCESS inside `success_s`. Outage signature is calls_window > 0 and
    success_window == 0 — busy but never successful, i.e. blocked or broken.

    Caveat: an unset outcome counts as a success here, so agents that never
    POST /v1/run/{run_id}/outcome can mask a real outage. Use
    outcome_missing_window to see who those are.
    """
    stats = sentinel_activity(activity_s, success_s)
    alerts = [s for s in stats if s["success_window"] == 0]
    return {
        "activity_s": activity_s,
        "success_s": success_s,
        "alert_count": len(alerts),
        "alerts": alerts,
        "agents": stats,
    }


@router.post("/admin/silence-canary/ack")
async def silence_canary_ack(agent: str, detail: str = ""):
    """Record that the canary fired for `agent` (used by the watchdog page so
    we don't re-page every run for the same standing outage)."""
    record_sentinel_event(agent, "silence_canary_page", detail)
    return {"agent": agent, "last_event_ts": last_sentinel_event(agent, "silence_canary_page")}


@router.post("/admin/halt")
async def admin_halt():
    halt()
    return {"status": "halted"}


@router.post("/admin/unlock")
async def admin_unlock():
    unlock()
    return {"status": "unlocked"}


@router.post("/v1/run/{run_id}/outcome")
async def run_outcome(run_id: str, request: Request):
    payload = await request.json()
    status = payload.get("status")
    if not status:
        raise HTTPException(400, "status is required")
    set_outcome(run_id, status, payload.get("artifact"))
    return {"run_id": run_id, "status": status}


@router.get("/admin/surge")
async def surge_status():
    surge = active_surge()
    if surge is None:
        return {"active": False}
    return {
        "active": True,
        "ends_at": surge.ends_at,
        "extra_cap": surge.extra_cap,
        "allow_frontier": surge.allow_frontier,
        "reason": surge.reason,
    }


@router.post("/admin/surge")
async def surge_start(request: Request):
    payload = await request.json()
    minutes = payload.get("minutes")
    extra_cap = payload.get("extra_cap")
    allow_frontier = bool(payload.get("allow_frontier", False))
    reason = payload.get("reason", "")
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        raise HTTPException(400, "minutes must be a positive number")
    if not isinstance(extra_cap, (int, float)) or extra_cap < 0:
        raise HTTPException(400, "extra_cap must be a non-negative number")

    decision = start_surge(minutes, extra_cap, allow_frontier, reason)
    if not decision.allowed:
        raise HTTPException(409, detail={"error": "surge_refused", "reason": decision.reason})

    async with httpx.AsyncClient() as client:
        await send_message(
            client,
            f"🚀 SURGE started | +${extra_cap:.2f} for {minutes:.0f}min | "
            f"frontier={'allowed' if allow_frontier else 'still 403'} | reason: {reason}",
        )
    return {
        "status": "started",
        "ends_at": decision.surge.ends_at,
        "extra_cap": decision.surge.extra_cap,
        "allow_frontier": decision.surge.allow_frontier,
    }


@router.post("/admin/surge/end")
async def surge_end():
    surge = end_surge()
    if surge is None:
        return {"status": "no_active_surge"}
    async with httpx.AsyncClient() as client:
        await send_message(client, f"⏹ SURGE ended early | reason was: {surge.reason}")
    return {"status": "ended", "reason": surge.reason}
