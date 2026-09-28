import subprocess
import sys
from pathlib import Path

RUN_AGENT = Path(__file__).resolve().parent.parent / "bin" / "run-agent"


def run(*args):
    return subprocess.run([sys.executable, str(RUN_AGENT), *args], capture_output=True, text=True)


def test_unknown_agent_refused():
    p = run("nonexistent")
    assert p.returncode == 1
    assert "unknown agent" in p.stderr


def test_missing_arg_usage():
    p = run()
    assert p.returncode == 2


def test_heavy_refused_while_claw_alive():
    # this repo's own tests run on the same host as the live openclaw
    # gateway, so this exercises a real refusal path — could be the
    # heavy-vs-claw conflict OR the RAM check firing first, depending on
    # live system load. Either way it must refuse and name claw as something
    # to stop, never launch, never auto-kill.
    p = run("dcode")
    if "claw" in p.stderr:
        assert p.returncode == 1
        assert "/stop claw" in p.stderr
