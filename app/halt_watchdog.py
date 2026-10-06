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

Out-of-process tick (cron / systemd timer)
------------------------------------------
The in-process loop only runs once llm-gate has been restarted with this
module wired in (see 2026-09-28 incident, gap #1 — the restart needs Andre).
Until that restart lands, the gap-#4 alerting would otherwise stay dormant.
`tick` / `run_tick` therefore re-use the *same* staleness rule from a
standalone process: `python -m app.halt_watchdog` reads the same HALT file and
pushes the same alert with no server restart. Dedup for those ticks is
persisted to a small JSON stamp under `STATE_DIR`, and the in-process loop
shares that same stamp so a cron tick and the loop can never double-alert.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from app.config import STATE_DIR, settings
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


STATE_FILE_NAME = "halt_watchdog_state.json"


def _state_path() -> Path:
    return Path(STATE_DIR) / STATE_FILE_NAME


def _load_last_alert(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("last_alert") if isinstance(data, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _save_last_alert(path: Path, value: float | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_alert": value}) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not persist halt-watchdog state at %s", path)


async def tick(
    client: httpx.AsyncClient,
    *,
    now: float | None = None,
    state_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """One watchdog tick backed by the on-disk dedup stamp.

    Shared by the in-process loop and the standalone CLI so both read and
    write the same `last_alert` stamp (no double alerts). The stamp is only
    written after a *successful* send, so a failed alert is retried on the
    next tick rather than swallowed for a full dedup window.
    """
    when = time.time() if now is None else now
    path = Path(state_path) if state_path is not None else _state_path()
    stale = evaluate_staleness(
        halt_status(),
        now=when,
        threshold_seconds=settings.halt_stale_threshold_hours * 3600,
    )
    report: dict = {
        "now": when,
        "stale": stale is not None,
        "alerted": False,
        "state_path": str(path),
        "dry_run": dry_run,
    }
    if stale is None:
        # Clear / fresh / un-ageable: re-arm so a release-then-reengage cannot
        # inherit the previous HALT's dedup window (up to 6h of silence).
        if not dry_run:
            _save_last_alert(path, None)
        return report

    report["age_seconds"] = stale["age_seconds"]
    last = _load_last_alert(path)
    if last is not None and (when - last) < _dedup_window_seconds():
        report["deduped"] = True
        return report

    if dry_run:
        report["would_alert"] = True
        return report

    try:
        await send_alert(client, format_alert(stale))
    except Exception:
        logger.exception("halt watchdog tick: alert send failed; will retry")
        report["error"] = "alert_send_failed"
        return report

    _save_last_alert(path, when)
    report["alerted"] = True
    return report


def run_tick(
    *,
    now: float | None = None,
    state_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Synchronous wrapper around `tick` for cron / systemd / CLI use."""

    async def _run() -> dict:
        async with httpx.AsyncClient() as client:
            return await tick(client, now=now, state_path=state_path, dry_run=dry_run)

    return asyncio.run(_run())


async def watchdog_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # File-backed dedup so the loop and any cron tick agree.
                await tick(client, state_path=_state_path())
            except Exception:
                logger.exception("halt watchdog crashed, will retry next interval")
            await asyncio.sleep(settings.halt_watchdog_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m app.halt_watchdog [--dry-run] [--state PATH]`."""
    parser = argparse.ArgumentParser(
        description="Standalone stale-HALT watchdog tick (no server restart needed)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick (default, kept for cron clarity)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate only; never alerts and never writes dedup state",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="path to the dedup stamp file (default: <STATE_DIR>/halt_watchdog_state.json)",
    )
    args = parser.parse_args(argv)

    report = run_tick(state_path=args.state, dry_run=args.dry_run)
    print(json.dumps(report, sort_keys=True))
    return 0


def _reset_for_test() -> None:
    global _last_halt_alert
    _last_halt_alert = None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
