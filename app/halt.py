"""Kill switch. ~/.llm-gate/HALT present -> 503 everything. Checked per-request."""
from __future__ import annotations

from app.config import HALT_FILE


def is_halted() -> bool:
    return HALT_FILE.exists()


def halt() -> None:
    HALT_FILE.touch()


def unlock() -> None:
    HALT_FILE.unlink(missing_ok=True)
