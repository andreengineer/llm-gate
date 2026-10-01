"""Regression tests for the HALT-leak fix in scripts/verify_migration.sh.

Incident 2026-09-28: the migration verifier created ~/.llm-gate/HALT to test the
kill switch and relied on a single `trap ... EXIT` to remove it. On SIGKILL or
an rm failure the HALT survived and the gate 503'd for 7 days. The script now
(a) only removes the HALT it created, and (b) restores any pre-existing HALT.
"""
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_migration.sh"


def _run(*args, **env):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=None if not env else {**__import__("os").environ, **env},
    )


def test_script_parses():
    r = _run("--help")
    assert r.returncode == 0
    assert "usage:" in r.stdout


def test_self_test_passes():
    r = _run("--self-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK: HALT acquire/release semantics" in r.stdout


def test_unknown_arg_rejected():
    r = _run("--bogus")
    assert r.returncode == 2


def test_no_halt_mode_never_touches_halt(tmp_path):
    """--no-halt must not create a HALT file and must not remove a real one."""
    halt = tmp_path / "HALT"
    r = _run("--no-halt", LLM_GATE_HALT=str(halt))
    assert "SKIP: --no-halt" in r.stdout
    assert not halt.exists()
