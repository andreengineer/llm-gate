"""Hard deny list. Never rewrite, fail loud. Checked before anything else."""
from __future__ import annotations

import fnmatch

DENY_PATTERNS: list[tuple[str, str]] = [
    ("openrouter/auto", "root cause of the $25 burn"),
    ("perplexity/sonar*", "per-request search fees invisible to blend gating; Perplexity Pro UI (sunk) strictly dominates"),
    ("*:online", "unpredictable per-request cost"),
    ("*:extended", "unpredictable per-request cost"),
]


def deny_reason(model: str) -> str | None:
    for pattern, reason in DENY_PATTERNS:
        if fnmatch.fnmatch(model, pattern):
            return reason
    return None


def check_fallback_array(models: list[str] | None, allowlist: set[str]) -> str | None:
    """documented bypass vector: any models:[] fallback array with a
    non-allowlisted entry is denied whole."""
    if not models:
        return None
    bad = [m for m in models if m not in allowlist]
    if bad:
        return f"fallback array contains non-allowlisted entries: {bad}"
    return None
