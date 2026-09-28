"""Ladder B — quality escalation (ESCALATION_POLICY.md §2).

Availability failure and quality failure are different events and must not
share an escalation path. Ladder A (app/upstreams.dispatch) is cost-flat: it
changes provider, never model, and a provider outage can never make a request
more expensive. Ladder B rises in cost, so it is gated three ways: only
deterministic triggers may fire it, it stops at auto_ceiling_blend, and it is
capped by a daily escalation count that is separate from dollar budgets.

The refusal of self-assessed escalation is hardcoded here on purpose. A model
judging its own answer insufficient has no ceiling and no auditor — it is
functionally openrouter/auto, which is on the hard deny list as the root cause
of the $25 burn. It must not be reachable by any spelling.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.ledger import escalation_count_today, record_escalation
from app.routing import above_ceiling, auto_ceiling_blend, max_auto_hops, quality_ladder

# §2.1 — the only triggers that may start a Ladder-B hop. Each is a fact about
# the RESPONSE or an explicit human action, never a judgement about quality.
SCHEMA_PARSE_FAILED = "schema_parse_failed"       # JSON schema required, response did not parse
TOOL_CALL_MALFORMED = "tool_call_malformed"       # tools declared, tool_call absent/malformed
EMPTY_AFTER_LADDER_A = "empty_after_ladder_a"     # empty from a HEALTHY provider, A exhausted
CALLER_REQUESTED = "caller_requested"             # X-Escalate: true — a human-initiated retry

PERMITTED_TRIGGERS = frozenset(
    {SCHEMA_PARSE_FAILED, TOOL_CALL_MALFORMED, EMPTY_AFTER_LADDER_A, CALLER_REQUESTED}
)

# §2.1 — forbidden, hardcoded. Anything that amounts to the model or an agent
# grading itself. Listed explicitly so a future caller inventing one of these
# spellings gets a refusal rather than a silent escalation.
FORBIDDEN_TRIGGERS = frozenset(
    {
        "low_confidence",
        "self_assessed",
        "self_assessment",
        "insufficient_answer",
        "model_unsatisfied",
        "needs_better_model",
        "retry",            # a generic retry with no classified failure
        "generic_retry",
        "quality",          # unclassified "it wasn't good enough"
    }
)


@dataclass(frozen=True)
class EscalationDecision:
    allowed: bool
    reason: str
    status: int | None = None
    code: str | None = None
    target_model: str | None = None
    target_provider: str | None = None
    needs_approval: bool = False


def is_permitted_trigger(trigger: str) -> bool:
    """A trigger is permitted only by being on the allowlist. An unknown
    trigger is refused rather than passed through — the failure mode we are
    guarding against is precisely a caller inventing a plausible-sounding
    reason."""
    return trigger in PERMITTED_TRIGGERS


def next_rung(current_model: str, hops_used: int) -> tuple[str, str] | None:
    """Next automatic quality rung, or None.

    LADDER_V2 §2: a quality escalation requires a STRICTLY increasing agentic
    index. Anything at or below the current index is a lateral move, not an
    escalation, and is never a target — that is what stops a "retry somewhere
    else" from being sold as an upgrade. Among rungs that do clear the current
    index we take the cheapest, so cost rises gradually rather than jumping to
    the smartest available.
    """
    if hops_used >= max_auto_hops():
        return None

    rungs = quality_ladder()
    current_index = None
    for r in rungs:
        if r.model == current_model:
            current_index = r.index
            break
    if current_index is None:
        current_index = -1

    eligible = [r for r in rungs if r.index > current_index]
    if not eligible:
        return None
    best = min(eligible, key=lambda r: (r.index, r.blend))
    return best.model, best.provider


def evaluate(agent: str, trigger: str, current_model: str, hops_used: int) -> EscalationDecision:
    """Decide whether one Ladder-B hop may happen. Order matters: refuse the
    forbidden triggers before spending any budget check on them."""
    if trigger in FORBIDDEN_TRIGGERS or not is_permitted_trigger(trigger):
        return EscalationDecision(
            False,
            f"trigger {trigger!r} is not a deterministic failure — self-assessed "
            "escalation has no ceiling and no auditor",
            400,
            "escalation_refused",
        )

    if escalation_count_today() >= settings.escalation_daily_max:
        return EscalationDecision(
            False,
            f"escalation budget exhausted ({settings.escalation_daily_max}/day) — a workload "
            "escalating this often means the cheap tier is wrong for its task class",
            429,
            "escalation_budget",
        )

    target = next_rung(current_model, hops_used)
    if target is None:
        # Nothing cheaper-adequate left under the ceiling. Anything above it is
        # approval-only; never hop there automatically.
        blocked = above_ceiling()
        if blocked and hops_used < max_auto_hops():
            return EscalationDecision(
                False,
                f"next rung exceeds auto_ceiling_blend ${auto_ceiling_blend():.2f}/M — "
                "approval required",
                None,
                "above_ceiling",
                target_model=blocked[0].model,
                target_provider=blocked[0].provider,
                needs_approval=True,
            )
        return EscalationDecision(False, "no further quality rung available", None, "ladder_exhausted")

    return EscalationDecision(True, "escalating", target_model=target[0], target_provider=target[1])


def commit(agent: str, chain_id: str, trigger: str, from_model: str, to_model: str) -> None:
    """Record an escalation that actually happened. Ladder A hops never call
    this — they are availability failover, not escalation."""
    record_escalation(agent, chain_id, trigger, from_model, to_model)
