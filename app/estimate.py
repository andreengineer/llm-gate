"""Rough pre-call cost estimate, used only to gate approval/budget BEFORE the
real call happens (real cost is metered from actual usage afterward)."""
from __future__ import annotations

from app.models_registry import ModelInfo

CHARS_PER_TOKEN = 4  # crude, deliberately conservative (overestimates input)


def estimate_cost(info: ModelInfo, messages: list[dict], max_tokens: int) -> float:
    input_chars = sum(len(str(m.get("content", ""))) for m in messages)
    input_tokens = max(1, input_chars // CHARS_PER_TOKEN)
    output_tokens = max_tokens
    return (input_tokens / 1_000_000) * info.input_per_m + (output_tokens / 1_000_000) * info.output_per_m
