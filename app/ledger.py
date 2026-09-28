"""SQLite spend ledger. Both upstreams write here — this is the layer that
catches loop burns regardless of which provider the money went to."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from app.config import LEDGER_DB, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    upstream TEXT NOT NULL,
    tier TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    prompt_hash TEXT NOT NULL,
    fallback_hop INTEGER NOT NULL DEFAULT 0,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_run ON calls(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent);
CREATE INDEX IF NOT EXISTS idx_calls_hash ON calls(prompt_hash);

-- migration for pre-existing production databases: CREATE TABLE IF NOT
-- EXISTS above is a no-op once the table already exists, so a genuinely
-- new column needs an explicit ALTER. SQLite has no "ADD COLUMN IF NOT
-- EXISTS", so this is done in Python below, tolerating "duplicate column".

CREATE TABLE IF NOT EXISTS would_block_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_wbe_ts ON would_block_events(ts);
CREATE INDEX IF NOT EXISTS idx_wbe_agent ON would_block_events(agent);

-- ESCALATION_POLICY.md §2.3. Escalation FREQUENCY is a product signal, not
-- just a cost event: a workload that escalates constantly means the cheap tier
-- is wrong for that task class. Counted separately from dollar budgets so a
-- cheap-but-constant escalator is still visible.
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    from_model TEXT NOT NULL,
    to_model TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS surges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    extra_cap REAL NOT NULL,
    allow_frontier INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_surges_started ON surges(started_at);

-- ENFORCE-mode refusals. Before this table existed, a request rejected by the
-- gate (dedup, loop, budget, deny-list, port-identity) wrote NOTHING: no calls
-- row, no would_block_events row (those are log_only-only). That is exactly how
-- 11 days of dedup_rejected 400s stayed invisible -- the only symptom was agent
-- silence. Every refusal now lands here AND as a calls row with a non-NULL
-- outcome, so "zero successful calls" is a queryable fact.
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    enforcement TEXT NOT NULL DEFAULT 'enforce'
);
CREATE INDEX IF NOT EXISTS idx_rejections_ts ON rejections(ts);
CREATE INDEX IF NOT EXISTS idx_rejections_agent ON rejections(agent);

-- SILENCE canary state. One row per evaluation that decided to page (or would
-- have, in dry-run). Doubles as the cooldown clock and as the audit trail CT
-- reads each night: "when did we last page, and for whom".
CREATE TABLE IF NOT EXISTS sentinel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sentinel_agent_ts ON sentinel_events(agent, ts);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(LEDGER_DB)
    try:
        conn.executescript(SCHEMA)
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN free_fallback INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        yield conn
        conn.commit()
    finally:
        conn.close()


def _day_start(now: float) -> float:
    return now - (now % 86400)


def _content_str(content) -> str:
    # OpenAI/Anthropic-style messages allow content as either a plain string
    # or a list of content blocks (text/image/tool parts) — LangChain-based
    # clients (dcode) send the latter even for plain text.
    if isinstance(content, list):
        return json.dumps(content, sort_keys=True)
    return (content or "").strip()


def prompt_hash(model: str, messages: list[dict]) -> str:
    normalized = "|".join(
        f"{m.get('role','')}:{_content_str(m.get('content'))}" for m in messages
    )
    return hashlib.sha256(f"{model}::{normalized}".encode()).hexdigest()


def record_call(
    run_id: str,
    agent: str,
    model: str,
    upstream: str,
    tier: str,
    cost_usd: float,
    p_hash: str,
    fallback_hop: bool = False,
    outcome: str | None = None,
    free_fallback: bool = False,
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO calls (ts, run_id, agent, model, upstream, tier, cost_usd, "
            "prompt_hash, fallback_hop, outcome, free_fallback) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), run_id, agent, model, upstream, tier, cost_usd, p_hash,
             int(fallback_hop), outcome, int(free_fallback)),
        )


def global_daily_spend(now: float | None = None) -> float:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE ts >= ?", (_day_start(now),)
        ).fetchone()
        return row[0]


def mid_tier_daily_spend(now: float | None = None) -> float:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE ts >= ? AND tier = 'mid'",
            (_day_start(now),),
        ).fetchone()
        return row[0]


def run_id_stats(run_id: str) -> tuple[int, float]:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row[0], row[1]


def agent_hourly_spend(agent: str, now: float | None = None) -> float:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE agent = ? AND ts >= ?",
            (agent, now - 3600),
        ).fetchone()
        return row[0]


def repeat_hash_count(p_hash: str, now: float | None = None) -> int:
    now = now or time.time()
    window = settings.repeat_hash_window_minutes * 60
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM calls WHERE prompt_hash = ? AND ts >= ?",
            (p_hash, now - window),
        ).fetchone()
        return row[0]


def chain_id(run_id: str, p_hash: str, now: float | None = None) -> str:
    """Stable id for one attempt chain (ROUTING_RESILIENCE.md §2). Retries of
    the same normalized body within `chain_window_seconds` land in the same
    time bucket and therefore inherit the same chain_id — so the gate's own
    3x-retry-plus-failover counts as ONE logical request, not N."""
    now = now if now is not None else time.time()
    bucket = int(now // settings.chain_window_seconds)
    raw = f"{run_id}:{p_hash}:{bucket}"
    return "c_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def chain_attempt_count(p_hash: str, now: float | None = None) -> int:
    """Attempts for one normalized body inside the chain window. Loop detection
    keys off this: a chain that exceeds `chain_max_attempts` is a real loop.
    Failover across upstreams is recorded once per incoming request (chat.py
    records the winning hop only), so legitimate failover never inflates it."""
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM calls WHERE prompt_hash = ? AND ts >= ?",
            (p_hash, now - settings.chain_window_seconds),
        ).fetchone()
        return row[0]


def run_chain_count(run_id: str) -> int:
    """Distinct attempt chains for a run — the per-run cap counts chains, not
    raw attempts (§2), so an agent's retries of one request don't burn the cap."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT prompt_hash) FROM calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row[0]


def record_escalation(
    agent: str, chain_id: str, trigger: str, from_model: str, to_model: str,
    now: float | None = None,
) -> None:
    """Record one Ladder-B (quality) hop. Ladder A hops are NOT escalations —
    they are cost-flat availability failover and must never land here."""
    now = now if now is not None else time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO escalations (ts, agent, chain_id, trigger, from_model, to_model)"
            " VALUES (?,?,?,?,?,?)",
            (now, agent, chain_id, trigger, from_model, to_model),
        )


def escalation_count_today(now: float | None = None) -> int:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM escalations WHERE ts >= ?", (_day_start(now),)
        ).fetchone()
        return row[0]


def escalations_by_trigger_today(now: float | None = None) -> dict[str, int]:
    """§2.3 daily digest: escalations by trigger type and by agent."""
    now = now or time.time()
    with _conn() as c:
        rows = c.execute(
            "SELECT trigger, agent, COUNT(*) FROM escalations WHERE ts >= ?"
            " GROUP BY trigger, agent ORDER BY 3 DESC",
            (_day_start(now),),
        ).fetchall()
    return {f"{trigger}/{agent}": n for trigger, agent, n in rows}


def fallback_hop_count_today(now: float | None = None) -> int:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM calls WHERE fallback_hop = 1 AND ts >= ?",
            (_day_start(now),),
        ).fetchone()
        return row[0]


def free_fallback_count_today(now: float | None = None) -> int:
    now = now or time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM calls WHERE free_fallback = 1 AND ts >= ?",
            (_day_start(now),),
        ).fetchone()
        return row[0]


def set_outcome(run_id: str, status: str, artifact: str | None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE calls SET outcome = ? WHERE run_id = ? AND id = "
            "(SELECT MAX(id) FROM calls WHERE run_id = ?)",
            (f"{status}:{artifact or ''}", run_id, run_id),
        )


def record_would_block(run_id: str, agent: str, reason: str, detail: str = "") -> None:
    """log_only ports: a budget/loop/dedup/approval check that WOULD have
    blocked, recorded instead of enforced. Queryable for the 24h review —
    deny-list and ui-only-tier blocks never go through this path, they
    always enforce regardless of port mode."""
    with _conn() as c:
        c.execute(
            "INSERT INTO would_block_events (ts, run_id, agent, reason, detail) VALUES (?,?,?,?,?)",
            (time.time(), run_id, agent, reason, detail),
        )


def would_block_summary(agent: str, since: float) -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT reason, COUNT(*) FROM would_block_events WHERE agent = ? AND ts >= ? GROUP BY reason",
            (agent, since),
        ).fetchall()
        return dict(rows)


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    status_code: int = 200
    error_code: str = ""


# --- surge mode: deliberate, bounded, logged cap escalation (SONNET.md §2) ---
# Provider balances are runway, not spending room — surge is the ONLY
# sanctioned way caps move, and it's always explicit/time-boxed/reasoned,
# never a silent threshold change. Per-run and loop-detection scopes never
# change during surge, regardless of extra_cap — a loop during surge is
# still a loop.
MAX_SURGES_PER_7_DAYS = 3


@dataclass
class Surge:
    id: int
    started_at: float
    ends_at: float
    extra_cap: float
    allow_frontier: bool
    reason: str


def active_surge(now: float | None = None) -> Surge | None:
    now = now if now is not None else time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT id, started_at, ends_at, extra_cap, allow_frontier, reason FROM surges "
            "WHERE ends_at > ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (now,),
        ).fetchone()
    if row is None:
        return None
    return Surge(id=row[0], started_at=row[1], ends_at=row[2], extra_cap=row[3],
                 allow_frontier=bool(row[4]), reason=row[5])


@dataclass
class SurgeDecision:
    allowed: bool
    reason: str = ""
    surge: Surge | None = None


def start_surge(minutes: float, extra_cap: float, allow_frontier: bool, reason: str) -> SurgeDecision:
    if not reason or not reason.strip():
        return SurgeDecision(False, "--reason is mandatory")
    now = time.time()
    if active_surge(now) is not None:
        return SurgeDecision(False, "a surge is already active — end it first (llm-gate surge --end)")

    with _conn() as c:
        recent = c.execute(
            "SELECT started_at, reason FROM surges WHERE started_at >= ? ORDER BY started_at DESC",
            (now - 7 * 86400,),
        ).fetchall()
    if len(recent) >= MAX_SURGES_PER_7_DAYS:
        reasons = "; ".join(f'"{r[1]}"' for r in recent[:MAX_SURGES_PER_7_DAYS])
        return SurgeDecision(
            False,
            f"max {MAX_SURGES_PER_7_DAYS} surges per 7 days reached — review the last "
            f"{MAX_SURGES_PER_7_DAYS} reasons first: {reasons}",
        )

    ends_at = now + minutes * 60
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO surges (started_at, ends_at, extra_cap, allow_frontier, reason) VALUES (?,?,?,?,?)",
            (now, ends_at, extra_cap, int(allow_frontier), reason),
        )
        surge_id = cur.lastrowid
    return SurgeDecision(True, surge=Surge(surge_id, now, ends_at, extra_cap, allow_frontier, reason))


def end_surge(now: float | None = None) -> Surge | None:
    now = now if now is not None else time.time()
    surge = active_surge(now)
    if surge is None:
        return None
    with _conn() as c:
        c.execute("UPDATE surges SET ended_at = ? WHERE id = ?", (now, surge.id))
    return surge


def effective_global_daily_hard(now: float | None = None) -> float:
    surge = active_surge(now)
    return settings.global_daily_hard + (surge.extra_cap if surge else 0.0)


def effective_mid_tier_daily_cap(now: float | None = None) -> float:
    surge = active_surge(now)
    return settings.mid_tier_daily_cap * 2 if surge else settings.mid_tier_daily_cap


def frontier_approval_allowed(now: float | None = None) -> bool:
    surge = active_surge(now)
    return bool(surge and surge.allow_frontier)


def check_budgets(agent: str, run_id: str, tier: str, est_cost: float, p_hash: str) -> BudgetDecision:
    """Pre-dispatch budget gate. Order matters: cheapest-to-explain reasons first."""
    now = time.time()

    # Loop detection counts attempts in the current chain (same normalized body
    # within the chain window), not raw prompt-hash repeats across all time —
    # so legitimate failover/retry stays under the bar and only a runaway trips
    # it (ROUTING_RESILIENCE.md §2).
    if chain_attempt_count(p_hash, now) >= settings.chain_max_attempts:
        return BudgetDecision(False, "attempt chain exceeded max attempts — real loop", 429, "loop_detected")

    # Per-run cap counts distinct chains, not attempts, for the same reason.
    _, cost = run_id_stats(run_id)
    if run_chain_count(run_id) >= settings.per_run_id_max_calls or cost + est_cost > settings.per_run_id_max_usd:
        return BudgetDecision(False, "run_id budget exhausted", 429, "run_budget_killed")

    # hermes-review (background_review.py's skill/memory curator fork) gets its
    # own smaller ceiling, isolated from hermes's own bucket — added 2026-09-17
    # after a stuck review turn's retries exhausted the shared hourly cap and
    # blocked live Telegram chat riding the same agent_id.
    hourly_cap = settings.review_agent_hourly_usd if agent == "hermes-review" else settings.per_agent_hourly_usd
    if agent_hourly_spend(agent, now) + est_cost > hourly_cap:
        return BudgetDecision(False, "per-agent hourly cap exceeded", 429, "agent_hourly_cap")

    if tier == "mid" and mid_tier_daily_spend(now) + est_cost > effective_mid_tier_daily_cap(now):
        return BudgetDecision(False, "mid tier daily aggregate cap exceeded", 429, "mid_tier_cap")

    if global_daily_spend(now) + est_cost > effective_global_daily_hard(now):
        return BudgetDecision(False, "global daily hard cap exceeded", 429, "global_daily_hard")

    return BudgetDecision(True)


# ---------------------------------------------------------------------------
# ENFORCE-rejection logging + silence-canary queries (2026-09-16, NG)
# ---------------------------------------------------------------------------

def stamp_outcome(run_id: str, outcome: str) -> int:
    """Stamp a terminal outcome on the run's still-unresolved `calls` rows.

    `record_call()` writes the spend row *before* the terminal status is known,
    so every dispatched request used to land with `outcome` NULL. The ledger
    health query scores NULL/'' as "outcome_missing", so a run that made zero
    *successful* calls was indistinguishable from a run that was never
    dispatched at all — the exact blind spot that hid the 11-day outage.

    Only NULL/empty rows are touched. A refusal already persisted through
    `record_rejection()` carries "rejected:<code>" and is never overwritten or
    double-counted. Returns the number of rows stamped.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE calls SET outcome = ? WHERE run_id = ? AND (outcome IS NULL OR outcome = '')",
            (outcome, run_id or "unknown"),
        )
        return cur.rowcount


def record_rejection(
    run_id: str,
    agent: str,
    reason_code: str,
    status_code: int,
    reason: str = "",
    enforcement: str = "enforce",
    model: str = "",
) -> None:
    """Persist a gate refusal so it is visible in metrics, not just in logs.

    Writes BOTH a `rejections` row (the why) and a `calls` row with a non-NULL
    `outcome` of the form "rejected:<code>" and cost 0 (the what). The calls row
    is what makes the silence canary possible: an agent whose calls are all
    "rejected:..." has had zero successful model calls, which is the exact
    condition that went undetected for 11 days.
    """
    now = time.time()
    outcome = f"rejected:{reason_code}"
    with _conn() as c:
        c.execute(
            "INSERT INTO rejections (ts, run_id, agent, status_code, reason_code, reason, enforcement)"
            " VALUES (?,?,?,?,?,?,?)",
            (now, run_id or "unknown", agent or "unknown", int(status_code), reason_code, reason[:500], enforcement),
        )
        c.execute(
            "INSERT INTO calls (ts, run_id, agent, model, upstream, tier, cost_usd, prompt_hash, fallback_hop, outcome)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, run_id or "unknown", agent or "unknown", model or "-", "gate", "rejected", 0.0, "", 0, outcome),
        )


def recent_rejections(since_hours: float = 24.0) -> list[tuple]:
    """(ts, agent, status_code, reason_code, run_id) newest first."""
    cutoff = time.time() - since_hours * 3600
    with _conn() as c:
        return list(
            c.execute(
                "SELECT ts, agent, status_code, reason_code, run_id FROM rejections"
                " WHERE ts >= ? ORDER BY ts DESC",
                (cutoff,),
            )
        )


def sentinel_activity(activity_s: float, success_s: float, now: float | None = None) -> list[dict]:
    """Per-agent window stats the silence canary reasons over.

    For every agent seen in the activity window: how many calls it made, how
    many SUCCEEDED, how many were rejected, and when its last call / last
    success happened. An agent with calls_in_window > 0 and success_s == 0 is
    the outage signature.
    """
    now = now or time.time()
    act_cut = now - activity_s
    suc_cut = now - success_s
    with _conn() as c:
        rows = c.execute(
            """
            SELECT agent,
                   SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END)                AS calls_window,
                   SUM(CASE WHEN ts >= ? AND outcome IS NOT NULL
                             AND outcome != '' AND outcome NOT LIKE 'rejected:%'
                            THEN 1 ELSE 0 END)                             AS success_window,
                   SUM(CASE WHEN ts >= ? AND outcome LIKE 'rejected:%'
                            THEN 1 ELSE 0 END)                             AS rejected_window,
                   SUM(CASE WHEN ts >= ? AND (outcome IS NULL OR outcome = '')
                            THEN 1 ELSE 0 END)                             AS outcome_missing_window,
                   MAX(ts)                                                 AS last_call_ts,
                   MAX(CASE WHEN outcome IS NOT NULL AND outcome != ''
                             AND outcome NOT LIKE 'rejected:%'
                            THEN ts END)                                   AS last_success_ts
              FROM calls
             GROUP BY agent
            HAVING calls_window > 0
            """,
            (act_cut, suc_cut, suc_cut, suc_cut),
        ).fetchall()
    out = []
    for agent, cw, sw, rw, mw, last_call, last_success in rows:
        out.append(
            {
                "agent": agent,
                "calls_window": int(cw or 0),
                "success_window": int(sw or 0),
                "rejected_window": int(rw or 0),
                "outcome_missing_window": int(mw or 0),
                "last_call_ts": last_call,
                "last_success_ts": last_success,
                "silent_for_s": (now - last_success) if last_success is not None else None,
            }
        )
    return out


def record_sentinel_event(agent: str, kind: str, detail: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO sentinel_events (ts, agent, kind, detail) VALUES (?,?,?,?)",
            (time.time(), agent, kind, detail[:500]),
        )


def last_sentinel_event(agent: str, kind_prefix: str) -> float | None:
    """ts of the newest event for agent whose kind starts with kind_prefix."""
    with _conn() as c:
        row = c.execute(
            "SELECT ts FROM sentinel_events WHERE agent=? AND kind LIKE ? ORDER BY ts DESC LIMIT 1",
            (agent, kind_prefix + "%"),
        ).fetchone()
    return row[0] if row else None

