"""Exact-match response cache + prompt trimming/dedup helpers (spec section 3).

Exact cache key = sha256(model + normalized_msgs + temp + tools). Skipped for
temp>0.3 or tool calls (non-deterministic / stateful). TTL 24h.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager

from app.config import CACHE_DB, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS exact_cache (
    key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")


@contextmanager
def _conn():
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_messages(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        content = (m.get("content") or "")
        if isinstance(content, list):
            content = json.dumps(content, sort_keys=True)
        content = content.strip()
        content = re.sub(r"[ \t]+\n", "\n", content)
        content = re.sub(r"\n{2,}", "\n", content)
        content = _ISO_TS_RE.sub("<ts>", content)
        parts.append(f"{m.get('role','')}:{content}")
    return "\n".join(parts)


def is_cacheable(temperature: float | None, tools: list | None) -> bool:
    if tools:
        return False
    if temperature is not None and temperature > 0.3:
        return False
    return True


def cache_key(model: str, messages: list[dict], temperature: float | None, tools: list | None) -> str:
    normalized = normalize_messages(messages)
    raw = f"{model}::{normalized}::{temperature}::{bool(tools)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get(key: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT response, created_at FROM exact_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    response_json, created_at = row
    if time.time() - created_at > settings.exact_cache_ttl_hours * 3600:
        return None
    return json.loads(response_json)


def put(key: str, response: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO exact_cache (key, response, created_at) VALUES (?,?,?)",
            (key, json.dumps(response), time.time()),
        )


# Spec §3 targets agents that concatenate retrieved documents without dedup.
# A line-set smaller than this cannot be a doc dump, and because dedup_ratio
# normalises by min(len(A), len(B)) any repeated one-liner (two identical tool
# acks, or a short message whose single line also occurs in the system prompt)
# scored 1.0 and hard-blocked the request on ENFORCE ports — killing every long
# agent loop. Only substantial blocks are eligible for the check.
DEDUP_MIN_LINES = 6


def _dedup_lines(text: str) -> set[str]:
    """Distinct non-blank lines, stripped — blank lines must not dilute overlap."""
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def dedup_ratio(a: str, b: str) -> float:
    """Cheap approximate duplicate ratio between two text blocks (line-set overlap).

    Returns 0.0 when either block has fewer than DEDUP_MIN_LINES distinct
    non-blank lines: such a block is a chat turn, not a concatenated doc dump,
    and min()-normalised overlap on tiny sets is a false-positive factory.
    """
    la, lb = _dedup_lines(a), _dedup_lines(b)
    if min(len(la), len(lb)) < DEDUP_MIN_LINES:
        return 0.0
    return len(la & lb) / min(len(la), len(lb))


def reject_if_duplicate_blocks(messages: list[dict], threshold: float = 0.6) -> str | None:
    """agents concatenating retrieved docs without dedup — reject if any two
    *user* message blocks in the same request are >60% duplicate.

    Only user-role turns are compared (ES 2026-09-16). The spec's failure mode is
    an agent stuffing the same retrieved document into successive *user* turns.
    assistant/tool/system turns legitimately repeat >=6-line status blocks, file
    listings and search-result tables during an agent loop; comparing them
    hard-blocked live production cron traffic (alpha-global-recon-daily: 10
    consecutive 400s on an enforce port, messages[25] vs messages[28]).
    Stale tool-result duplication is already collapsed by trim_old_tool_results().
    """
    contents = [
        (i, m.get("content"))
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m.get("content")
    ]
    for x in range(len(contents)):
        for y in range(x + 1, len(contents)):
            i, a = contents[x]
            j, b = contents[y]
            if dedup_ratio(a, b) > threshold:
                return f"messages[{i}] and messages[{j}] are >{int(threshold*100)}% duplicate"
    return None


def trim_old_tool_results(messages: list[dict], keep_recent_turns: int = 3) -> list[dict]:
    """tool-results >3 turns old -> 1-line summary. Truncate MIDDLE never head —
    head truncation kills the cache prefix."""
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent_turns:
        return messages
    stale = set(tool_indices[:-keep_recent_turns])
    trimmed = []
    for i, m in enumerate(messages):
        if i in stale:
            content = m.get("content") or ""
            if not isinstance(content, str):
                content = json.dumps(content)
            summary = content[:80].replace("\n", " ")
            trimmed.append({**m, "content": f"[trimmed tool result: {summary}...]"})
        else:
            trimmed.append(m)
    return trimmed
