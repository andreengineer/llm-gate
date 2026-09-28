"""Mid-tier approval + alerts. SEPARATE bot token from the Hermes gateway bot
by design — the approval channel must not share fate with the throttled
channel (if Hermes's own bot gets rate-limited/killed, approvals still work).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

import httpx

from app.config import settings

logger = logging.getLogger("llm-gate.telegram")

API = "https://api.telegram.org/bot{token}/{method}"

# agent+model combos approved for a time window via /approve_<id>_30m
_temp_approved: dict[tuple[str, str], float] = {}
_last_update_id: int = 0


def _configured() -> bool:
    return bool(settings.telegram_approval_bot_token and settings.telegram_approval_chat_id)


async def send_message(client: httpx.AsyncClient, text: str) -> None:
    if not _configured():
        logger.warning("telegram not configured, message dropped: %s", text)
        return
    url = API.format(token=settings.telegram_approval_bot_token, method="sendMessage")
    try:
        await client.post(url, json={"chat_id": settings.telegram_approval_chat_id, "text": text}, timeout=10.0)
    except httpx.HTTPError as exc:
        logger.error("telegram send failed: %s", exc)


async def send_alert(client: httpx.AsyncClient, text: str) -> None:
    await send_message(client, f"⚠️ {text}")


# no_live_route alerts are deduplicated (ROUTING_RESILIENCE §4): a total outage
# means every request 503s, and one alert per request would be a flood. Send at
# most one per dedup window.
_last_no_live_route_alert: float = 0.0


async def alert_no_live_route(client: httpx.AsyncClient, chain_id: str, next_action: str) -> None:
    global _last_no_live_route_alert
    now = time.time()
    window = settings.no_live_route_alert_dedup_minutes * 60
    if now - _last_no_live_route_alert < window:
        return
    _last_no_live_route_alert = now
    await send_alert(
        client,
        f"NO LIVE ROUTE — every upstream is down. chain {chain_id}\n"
        f"next: {next_action}\nsee GET /admin/health",
    )


def _reset_no_live_route_dedup_for_test() -> None:
    global _last_no_live_route_alert
    _last_no_live_route_alert = 0.0


def is_temp_approved(agent: str, model: str) -> bool:
    expiry = _temp_approved.get((agent, model))
    return expiry is not None and time.time() < expiry


async def _poll_for_decision(client: httpx.AsyncClient, approval_id: str, deadline: float) -> str | None:
    global _last_update_id
    approve_cmd = f"/approve_{approval_id}"
    deny_cmd = f"/deny_{approval_id}"
    approve_30m_cmd = f"/approve_{approval_id}_30m"

    while time.time() < deadline:
        url = API.format(token=settings.telegram_approval_bot_token, method="getUpdates")
        try:
            resp = await client.get(
                url,
                params={"offset": _last_update_id + 1, "timeout": 5},
                timeout=10.0,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except httpx.HTTPError as exc:
            logger.error("telegram poll failed: %s", exc)
            await asyncio.sleep(2)
            continue

        for update in updates:
            _last_update_id = max(_last_update_id, update["update_id"])
            text = (update.get("message", {}) or {}).get("text", "") or ""
            text = text.strip()
            if text == approve_30m_cmd:
                return "approve_30m"
            if text == approve_cmd:
                return "approve"
            if text == deny_cmd:
                return "deny"

        if not updates:
            await asyncio.sleep(2)

    return None


async def request_mid_tier_approval(
    client: httpx.AsyncClient,
    agent: str,
    run_id: str,
    model: str,
    est_cost: float,
    mid_spent_today: float,
    global_spent_today: float,
    prompt_preview: str,
) -> bool:
    """Returns True iff approved. ALWAYS denies on timeout — no exceptions."""
    if is_temp_approved(agent, model):
        return True

    if not _configured():
        logger.error("mid-tier approval required but telegram not configured -> deny")
        return False

    approval_id = secrets.token_hex(2)
    from app.models_registry import registry

    info = registry.get(model)
    blend = info.blend if info else 0.0
    text = (
        f"APPROVAL #{approval_id} | agent {agent} | run {run_id}\n"
        f"model {model} (mid ${blend:.2f}/M) | est ${est_cost:.4f}\n"
        f"mid-tier today: ${mid_spent_today:.2f} / ${settings.mid_tier_daily_cap:.2f} | "
        f"global: ${global_spent_today:.2f} / ${settings.global_daily_hard:.2f}\n"
        f"prompt: \"{prompt_preview[:200]}\"\n"
        f"/approve_{approval_id}  /deny_{approval_id}  /approve_{approval_id}_30m"
    )
    await send_message(client, text)

    deadline = time.time() + settings.approval_timeout_seconds
    decision = await _poll_for_decision(client, approval_id, deadline)

    if decision == "approve":
        return True
    if decision == "approve_30m":
        _temp_approved[(agent, model)] = time.time() + 30 * 60
        return True
    return False  # deny, or None (timeout) -> always DENY
