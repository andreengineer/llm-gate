"""Kill switch. ~/.llm-gate/HALT present -> 503 everything. Checked per-request.

Audit trail (2026-09-28 incident): the switch used to be a bare `touch` /
`unlink`, so an accidental HALT left no trace of who set it, when, or why --
production 503'd for days and nobody could tell how long it had been on. Now:

  * the HALT file itself carries {"ts", "actor", "reason"} as JSON,
  * every engage/release appends a JSON line to HALT.log (append-only), and
  * halt_status() exposes the age so monitoring can flag a *stale* HALT
    instead of rediscovering it from a wall of 503s.

`is_halted()` keeps the exact same semantics (file exists == halted), so a HALT
created the old way with a plain `touch` still works -- halt_status() degrades
gracefully to actor="unknown" for legacy files.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.config import HALT_FILE, STATE_DIR

HALT_LOG_FILE = STATE_DIR / "HALT.log"


def _record(action: str, actor: str, reason: str) -> None:
    """Append one audit line. Never let audit failure break the kill switch."""
    line = json.dumps(
        {
            "ts": time.time(),
            "action": action,
            "actor": actor or "unknown",
            "reason": reason or "",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    try:
        with HALT_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def is_halted() -> bool:
    return HALT_FILE.exists()


def halt(reason: str = "", actor: str = "unknown") -> None:
    """Engage the kill switch; record who/why in the file and the audit log."""
    HALT_FILE.write_text(
        json.dumps(
            {"ts": time.time(), "actor": actor or "unknown", "reason": reason or ""},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _record("halt", actor, reason)


def unlock(actor: str = "unknown", reason: str = "") -> None:
    """Release the kill switch. Only audit-log when something was actually set."""
    existed = is_halted()
    HALT_FILE.unlink(missing_ok=True)
    if existed:
        _record("unlock", actor, reason)


def halt_status() -> dict[str, Any]:
    """Metadata for the current HALT; {"halted": False} when clear.

    Tolerates legacy/empty HALT files (plain `touch`) and corrupt JSON.
    """
    if not HALT_FILE.exists():
        return {"halted": False}

    info: dict[str, Any] = {"halted": True}
    meta: dict[str, Any] = {}
    try:
        raw = HALT_FILE.read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
    except (OSError, json.JSONDecodeError):
        meta = {}

    info["actor"] = meta.get("actor") or "unknown"
    info["reason"] = meta.get("reason") or ""
    ts = meta.get("ts")
    info["since"] = ts if isinstance(ts, (int, float)) else None
    if isinstance(ts, (int, float)):
        info["age_seconds"] = max(0.0, time.time() - ts)
    return info
