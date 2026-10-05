"""Tests for the stale-HALT watchdog (incident 2026-09-28, gap #4)."""

from __future__ import annotations

import app.halt_watchdog as hw
from app.config import settings


def _status(*, halted: bool, since: float | None = None, ts: bool = True, **extra):
    s: dict = {"halted": halted, "actor": "andre", "reason": "cost spike"}
    if since is not None:
        s["since"] = since
        if ts:
            s["age_seconds"] = 0.0  # halt_status recomputes; loop uses `since`
    return s


def test_clear_not_stale():
    assert hw.evaluate_staleness(
        _status(halted=False), now=10_000, threshold_seconds=3600
    ) is None


def test_fresh_halt_not_stale():
    assert hw.evaluate_staleness(
        _status(halted=True, since=10_000 - 60), now=10_000, threshold_seconds=3600
    ) is None


def test_stale_halt_reported_with_age():
    out = hw.evaluate_staleness(
        _status(halted=True, since=10_000 - 7 * 3600),
        now=10_000,
        threshold_seconds=3600,
    )
    assert out is not None
    assert out["age_seconds"] == 7 * 3600
    assert out["actor"] == "andre"
    assert out["reason"] == "cost spike"


def test_legacy_halt_without_age_is_not_stale():
    # No `since`, no `age_seconds` -> cannot age it, must not false-alarm.
    status = {"halted": True, "actor": "andre", "reason": "legacy"}
    assert hw.evaluate_staleness(status, now=10_000, threshold_seconds=1) is None


class _FakeStatus:
    def __init__(self, status):
        self.status = status

    def __call__(self):
        return self.status


def test_check_once_dedups_then_realerts(monkeypatch):
    hw._reset_for_test()
    now = 1_000_000.0
    monkeypatch.setattr(
        hw,
        "halt_status",
        _FakeStatus(_status(halted=True, since=now - 24 * 3600)),
    )

    first = hw.check_once(now=now)
    assert first is not None and first["age_seconds"] > 0

    # inside the dedup window -> silent
    assert hw.check_once(now=now + 60) is None

    # after the dedup window, still stale -> re-alert
    window = settings.halt_watchdog_alert_dedup_minutes * 60
    assert hw.check_once(now=now + window + 1) is not None


def test_check_once_resets_on_release(monkeypatch):
    hw._reset_for_test()
    now = 2_000_000.0
    fake = _FakeStatus(_status(halted=True, since=now - 24 * 3600))
    monkeypatch.setattr(hw, "halt_status", fake)
    assert hw.check_once(now=now) is not None

    fake.status = _status(halted=False)
    assert hw.check_once(now=now + 30) is None
    # released then re-engaged -> alerts again immediately
    fake.status = _status(halted=True, since=now + 31 - 24 * 3600)
    assert hw.check_once(now=now + 31) is not None


def test_format_alert_mentions_release():
    msg = hw.format_alert(
        {"age_seconds": 7 * 3600, "actor": "andre", "reason": "cost", "since": 0}
    )
    assert "STALE HALT" in msg
    assert "/admin/unlock" in msg
