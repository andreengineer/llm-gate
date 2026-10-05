"""Stale-HALT watchdog (2026-09-28 incident, gap #4).

halt.py already exposes *how long* a HALT has been on, but nothing watched that
metadata: a forgotten HALT 503s every request for days and the only tell is the
wall of 503s itself. This loop turns the age into a push — once a HALT has been
engaged longer than `halt_stale_threshold_hours`, it alerts the approval channel,
then re-alerts at most every `halt_watchdog_alert_dedup_minutes` so a HALT that
stays engaged keeps nagging instead of going quiet after one alert.

The decision logic is a pure function (`evaluate_staleness`) plus a stateless
dedup check, so both are unit-testable without a clock, a network, or a live
HALT file. `check_once` is the single-tick entry point the loop and the tests
share.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import settings
from app.halt import halt_status
from app.telegram import send_alert

logger = logging.getLogger(__name__)

# At most one stale-HALT alert per dedup window; re-armed while the HALT stays
# engaged. Mirrors telegram._last_no_live_route_alert.
_last_halt_alert: float | None = None


def evaluate_staleness(
    status: dict, *, now: float, threshold_seconds: float
) -> dict | None:
    """Return a stale-HALT description, or None if clear / fresh / un-ageable.

    `status` is halt.halt_status(). A HALT whose age cannot be determined (legacy
    or empty file with no `ts`) is *not* reported: we cannot claim it is stale,
    and the audit trail now records `ts` on every engage, so only pre-upgrade
    HALTs fall in this bucket.
    """
    if not status.get("halted"):
        return None

    since = status.get("since")
    if isinstance(since, (int, float)):
        age = max(0.0, now - since)
    else:
        age = status.get("age_seconds")
    if not isinstance(age, (int, float)) or age < threshold_seconds:
        return None

    return {
        "age_seconds": float(age),
        "actor": status.get("actor") or "unknown",
        "reason": status.get("reason") or "",
        "since": since,
    }


def _dedup_window_seconds() -> float:
    return settings.halt_watchdog_alert_dedup_minutes * 60


def check_once(now: float | None = None) -> dict | None:
    """One watchdog tick. Returns the stale dict (alert due) or None.

    Updates the module-level dedup stamp only when it returns a dict, so the
    async loop can send exactly one alert per due tick.
    """
    global _last_halt_alert
    when = time.time() if now is None else now
    stale = evaluate_staleness(
        halt_status(),
        now=when,
        threshold_seconds=settings.halt_stale_threshold_hours * 3600,
    )
    if stale is None:
        # Clear / fresh / un-ageable: re-arm so a release-then-reengage cannot
        # inherit the previous HALT's dedup window (up to 6h of silence).
        _last_halt_alert = None
        return None
    if _last_halt_alert is not None and (when - _last_halt_alert) < _dedup_window_seconds():
        return None
    _last_halt_alert = when
    return stale


def format_alert(stale: dict) -> str:
    hours = stale["age_seconds"] / 3600.0
    return (
        f"STALE HALT — engaged {hours:.1f}h (> "
        f"{settings.halt_stale_threshold_hours}h).\n"
        f"actor: {stale['actor']}\n"
        f"reason: {stale['reason'] or '(none)'}\n"
        f"release: POST /admin/unlock (rm ~/.llm-gate/HALT)"
    )


async def watchdog_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                stale = check_once()
                if stale is not None:
                    await send_alert(client, format_alert(stale))
            except Exception:
                logger.exception("halt watchdog crashed, will retry next interval")
            await asyncio.sleep(settings.halt_watchdog_interval_seconds)


def _reset_for_test() -> None:
    global _last_halt_alert
    _last_halt_alert = None
