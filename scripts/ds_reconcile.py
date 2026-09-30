#!/usr/bin/env python3
"""I7_MAIN.md §3 — daily DeepSeek reconcile (run by ds-reconcile.timer, 00:05 UTC).

DeepSeek has no usage API, only GET /user/balance. So: store the balance each
run, and compare the balance drop since the previous run ("billed") with what
the gate's ledger recorded for upstream=deepseek over the same window.

  ratio = ledger / billed      healthy band 0.8 .. 1.25
  gap   = billed - ledger      "unmetered" (e.g. a key that bypasses the gate)

Alert (Telegram + healthchecks /fail) only when the ratio is out of band AND
the absolute gap exceeds $0.50/day — small absolute noise (the balance has
cent resolution) is not worth a page. A balance that went UP means a top-up
landed inside the window; that day cannot be reconciled and is reported as such.

First run only records a baseline. Writes the latest result to
~/.llm-gate/ds_reconcile.json for the digest.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
GATE_ENV = HOME / "llm-gate" / ".env"
STACK_ENV = HOME / ".stack.env"
STATE_DIR = Path(os.environ.get("LLM_GATE_STATE_DIR", HOME / ".llm-gate"))
LEDGER_DB = STATE_DIR / "spend.db"
RESULT_FILE = STATE_DIR / "ds_reconcile.json"
NOTIFY = HOME / "stack-ops" / "deploy" / "notify.sh"

BAND = (0.8, 1.25)
GAP_ALERT_USD = 0.50


def _env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _balance(key: str) -> float:
    req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return sum(float(b["total_balance"]) for b in data["balance_infos"] if b["currency"] == "USD")


def _hc(base: str, fail: bool) -> None:
    if not base:
        return
    try:
        urllib.request.urlopen(f"{base}/ds-reconcile{'/fail' if fail else ''}", timeout=10)
    except OSError:
        pass


def _notify(msg: str) -> None:
    subprocess.run([str(NOTIFY), msg], check=False, timeout=30)


def main() -> int:
    hc_base = _env(STACK_ENV).get("HC_BASE_URL", "")
    key = _env(GATE_ENV).get("DEEPSEEK_API_KEY", "")
    if not key:
        print("no DEEPSEEK_API_KEY in ~/llm-gate/.env", file=sys.stderr)
        _hc(hc_base, fail=True)
        return 1

    now = time.time()
    try:
        balance = _balance(key)
    except Exception as exc:  # network / API shape
        _notify(f"ds-reconcile: balance fetch failed: {exc}")
        _hc(hc_base, fail=True)
        return 1

    with sqlite3.connect(LEDGER_DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS ds_balance (ts REAL NOT NULL, balance_usd REAL NOT NULL)")
        prev = c.execute("SELECT ts, balance_usd FROM ds_balance ORDER BY ts DESC LIMIT 1").fetchone()
        c.execute("INSERT INTO ds_balance (ts, balance_usd) VALUES (?, ?)", (now, balance))
        ledger = None
        if prev:
            ledger = c.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE upstream='deepseek' AND ts > ? AND ts <= ?",
                (prev[0], now),
            ).fetchone()[0]

    result: dict = {"ts": now, "balance_usd": balance}
    fail = False
    if prev is None:
        result["status"] = "baseline"
        line = f"ds-reconcile: baseline recorded, balance ${balance:.2f}"
    else:
        billed = prev[1] - balance
        hours = (now - prev[0]) / 3600
        result.update(window_hours=round(hours, 1), billed_usd=round(billed, 4),
                      ledger_usd=round(ledger, 4))
        if billed < 0:
            result["status"] = "topup"
            line = (f"ds-reconcile: balance rose ${-billed:.2f} over {hours:.0f}h (top-up) — "
                    f"window not reconcilable; ledger ${ledger:.4f}")
        else:
            gap = billed - ledger
            ratio = (ledger / billed) if billed > 0 else (1.0 if ledger < 0.01 else float("inf"))
            out_of_band = not (BAND[0] <= ratio <= BAND[1])
            fail = out_of_band and abs(gap) > GAP_ALERT_USD
            result.update(ratio=round(ratio, 3), unmetered_usd=round(gap, 4),
                          status="alert" if fail else "ok")
            line = (f"ds-reconcile {hours:.0f}h: billed ${billed:.2f}, ledger ${ledger:.4f}, "
                    f"ratio {ratio:.2f} (band {BAND[0]}-{BAND[1]}), unmetered ${gap:.2f}")
            if fail:
                line += " — OUT OF BAND"

    RESULT_FILE.write_text(json.dumps(result, indent=1))
    print(line)
    if fail:
        _notify(line)
    _hc(hc_base, fail=fail)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
