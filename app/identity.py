"""Run-id synthesis for ports where the agent can't set X-Run-Id itself
(GATE_MIGRATION_PLAN.md Step 2). Coarse — same system prompt within the same
hour collapses to one run_id — but it's enough to keep per-run caps and loop
detection working instead of going completely meterless."""
from __future__ import annotations

import hashlib
import time


def _first_system_prompt(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "system":
            return str(m.get("content") or "")
    if messages:
        return str(messages[0].get("content") or "")
    return ""


def synthesize_run_id(agent_id: str, messages: list[dict], now: float | None = None) -> str:
    now = now if now is not None else time.time()
    digest = hashlib.sha256(_first_system_prompt(messages).encode()).hexdigest()[:8]
    hour_bucket = int(now // 3600)
    return f"{agent_id}_{digest}_{hour_bucket}"
