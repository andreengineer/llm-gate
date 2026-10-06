"""Tests for the out-of-process (cron/systemd) stale-HALT tick.

The in-process loop needs a restart to go live (incident 2026-09-28, gap #1).
`run_tick` / `main` deliver the same gap-#4 alerting from a standalone process
using a persisted dedup stamp, so it can run today without the restart.
"""

from __future__ import annotations

import asyncio
import json

import app.halt_watchdog as hw
from app.config import settings


def _stale(now: float, *, hours: float = 24.0) -> dict:
    return {
        "halted": True,
        "actor": "andre",
        "reason": "cost spike",
        "since": now - hours * 3600,
        "age_seconds": hours * 3600,
    }


class _FakeStatus:
    def __init__(self, status: dict):
        self.status = status

    def __call__(self) -> dict:
        return self.status


def _spy_sender(calls: list[tuple[str, str]]):
    async def _send(client, message: str) -> None:
        calls.append(("alert", message))

    return _send


def test_run_tick_alerts_then_dedups_across_processes(tmp_path, monkeypatch):
    now = 5_000_000.0
    monkeypatch.setattr(hw, "halt_status", _FakeStatus(_stale(now)))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hw, "send_alert", _spy_sender(calls))
    state = tmp_path / "watchdog.json"

    first = hw.run_tick(now=now, state_path=state)
    assert first["stale"] is True and first["alerted"] is True
    assert len(calls) == 1 and "STALE HALT" in calls[0][1]
    # stamped only after a successful send
    assert json.loads(state.read_text())["last_alert"] == now

    # second tick inside the window -> silent, but still reports staleness
    second = hw.run_tick(now=now + 60, state_path=state)
    assert second["stale"] is True and second["alerted"] is False
    assert second.get("deduped") is True
    assert len(calls) == 1

    # past the window -> alerts again
    window = settings.halt_watchdog_alert_dedup_minutes * 60
    third = hw.run_tick(now=now + window + 1, state_path=state)
    assert third["alerted"] is True
    assert len(calls) == 2


def test_run_tick_rearms_after_release(tmp_path, monkeypatch):
    now = 6_000_000.0
    fake = _FakeStatus(_stale(now))
    monkeypatch.setattr(hw, "halt_status", fake)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hw, "send_alert", _spy_sender(calls))
    state = tmp_path / "watchdog.json"

    assert hw.run_tick(now=now, state_path=state)["alerted"] is True
    # a clear HALT clears the stamp so a re-engage alerts immediately
    fake.status = {"halted": False}
    assert hw.run_tick(now=now + 30, state_path=state)["stale"] is False
    assert json.loads(state.read_text())["last_alert"] is None

    fake.status = _stale(now + 31)
    assert hw.run_tick(now=now + 31, state_path=state)["alerted"] is True
    assert len(calls) == 2


def test_run_tick_dry_run_sends_nothing_and_writes_nothing(tmp_path, monkeypatch):
    now = 7_000_000.0
    monkeypatch.setattr(hw, "halt_status", _FakeStatus(_stale(now)))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hw, "send_alert", _spy_sender(calls))
    state = tmp_path / "watchdog.json"

    report = hw.run_tick(now=now, state_path=state, dry_run=True)
    assert report["would_alert"] is True and report["alerted"] is False
    assert calls == []
    assert not state.exists()


def test_run_tick_does_not_stamp_when_send_fails(tmp_path, monkeypatch):
    now = 8_000_000.0
    monkeypatch.setattr(hw, "halt_status", _FakeStatus(_stale(now)))
    state = tmp_path / "watchdog.json"

    async def _boom(client, message: str) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(hw, "send_alert", _boom)
    failed = hw.run_tick(now=now, state_path=state)
    assert failed["alerted"] is False and failed["error"] == "alert_send_failed"
    # not stamped -> the next tick retries instead of staying silent for the window
    assert not state.exists() or json.loads(state.read_text())["last_alert"] is None

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(hw, "send_alert", _spy_sender(calls))
    assert hw.run_tick(now=now + 60, state_path=state)["alerted"] is True
    assert len(calls) == 1


def test_main_cli_dry_run_emits_json(tmp_path, monkeypatch, capsys):
    now = 9_000_000.0
    monkeypatch.setattr(hw, "halt_status", _FakeStatus(_stale(now)))
    state = tmp_path / "watchdog.json"

    rc = hw.main(["--dry-run", "--state", str(state)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["would_alert"] is True and out["state_path"] == str(state)
