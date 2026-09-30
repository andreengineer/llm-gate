"""HALT must leave an audit trail.

Regression guard for the 2026-09-28 incident: the kill switch was a bare
``touch``/``unlink``, so a forgotten HALT 503'd production for days with no
record of who set it, when, or why. These tests pin the audit contract:

  * the HALT file carries {"ts", "actor", "reason"},
  * every engage/release appends a JSON line to HALT.log,
  * /health surfaces the live HALT age (so a stale HALT is visible),
  * legacy ``touch``-created files still halt and degrade gracefully.
"""
import json
import time

import httpx
import pytest

from app import halt as halt_mod
from app.main import app


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gate:8787"
    )


def _reset_log():
    halt_mod.HALT_LOG_FILE.unlink(missing_ok=True)


def _log_lines():
    if not halt_mod.HALT_LOG_FILE.exists():
        return []
    return [
        json.loads(line)
        for line in halt_mod.HALT_LOG_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_halt_records_actor_reason_and_logs():
    _reset_log()
    async with await _client() as c:
        resp = await c.post(
            "/admin/halt", params={"reason": "cost spike", "actor": "andre"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "halted"
    assert body["halt"]["halted"] is True
    assert body["halt"]["actor"] == "andre"
    assert body["halt"]["reason"] == "cost spike"
    assert body["halt"]["age_seconds"] >= 0

    lines = _log_lines()
    assert lines[-1]["action"] == "halt"
    assert lines[-1]["actor"] == "andre"
    assert lines[-1]["reason"] == "cost spike"


@pytest.mark.asyncio
async def test_health_surfaces_live_halt_age():
    halt_mod.halt(reason="manual freeze", actor="andre")
    time.sleep(0.01)
    async with await _client() as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "halted"
    assert body["halt"]["halted"] is True
    assert body["halt"]["reason"] == "manual freeze"
    assert body["halt"]["actor"] == "andre"
    assert body["halt"]["age_seconds"] > 0


@pytest.mark.asyncio
async def test_unlock_logs_release_and_clears():
    _reset_log()
    halt_mod.halt(reason="x", actor="andre")
    async with await _client() as c:
        resp = await c.post(
            "/admin/unlock", params={"reason": "resolved", "actor": "andre"}
        )
    body = resp.json()
    assert body["status"] == "unlocked"
    assert body["halt"] == {"halted": False}
    assert [line["action"] for line in _log_lines()] == ["halt", "unlock"]


def test_legacy_touch_halt_still_halts_and_degrades():
    halt_mod.HALT_FILE.write_text("", encoding="utf-8")  # old-style plain touch
    assert halt_mod.is_halted() is True
    st = halt_mod.halt_status()
    assert st["halted"] is True
    assert st["actor"] == "unknown"
    assert st["reason"] == ""
    assert st["since"] is None


def test_unlock_when_clear_is_silent():
    _reset_log()
    halt_mod.unlock()
    assert halt_mod.is_halted() is False
    assert _log_lines() == []
