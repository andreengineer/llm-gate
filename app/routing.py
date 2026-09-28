"""Provider escalation ladder loader (ROUTING_RESILIENCE.md §3/§4).

The ladder is data, not code: an ordered list of rungs in routing.yaml, each
naming a provider (health-cache key), a concrete model, a tier, and whether a
client-facing path may fail over onto it. This module only loads and filters —
health filtering and dispatch live in app/health.py and app/upstreams.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from app.config import ROUTING_YAML_PATH

# Providers a request may be routed to. Every rung's `provider` must be one of
# these — they are also the keys the health cache tracks.
KNOWN_PROVIDERS = {"deepseek", "aistudio", "groq", "openrouter"}


@dataclass(frozen=True)
class Rung:
    rung: int
    provider: str      # health-cache key / real upstream account
    model: str         # concrete model id to call on that provider
    tier: str          # "free" | "cheap" — ladder never contains mid/frontier
    free: bool         # True for :free / free-tier models
    client_ok: bool    # client-facing (isaura) may fail over onto this rung
    index: int = 0     # agentic-intelligence index; see min_index (LADDER_V2 §1)


class IndexFloorError(ValueError):
    """A configured rung scores below min_index. LADDER_V2 §1: models under the
    floor are excluded from config entirely, not merely deprioritised, so this
    is a hard boot failure rather than a filter."""


def _load(path=ROUTING_YAML_PATH) -> tuple[list[Rung], dict[str, str]]:
    raw = yaml.safe_load(path.read_text())
    min_index = int(raw.get("min_index", 0))
    rungs: list[Rung] = []
    for spec in raw.get("ladder", []):
        provider = spec["provider"]
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"routing.yaml rung {spec.get('rung')} names unknown provider "
                f"{provider!r}; known: {sorted(KNOWN_PROVIDERS)}"
            )
        index = int(spec.get("index", 0))
        free = bool(spec.get("free", False))
        # Free rungs are floor-exempt: $0 changes the calculus (LADDER_V2 §2.1).
        if not free and index < min_index:
            raise IndexFloorError(
                f"routing.yaml rung {spec.get('rung')} ({spec['model']} @ {provider}) "
                f"has index {index}, below min_index {min_index}"
            )
        rungs.append(
            Rung(
                rung=int(spec["rung"]),
                provider=provider,
                model=spec["model"],
                tier=spec.get("tier", "cheap"),
                free=free,
                client_ok=bool(spec.get("client_ok", False)),
                index=index,
            )
        )
    rungs.sort(key=lambda r: r.rung)
    remediation = dict(raw.get("remediation", {}))
    return rungs, remediation


@dataclass(frozen=True)
class QualityRung:
    model: str
    provider: str
    blend: float       # USD/M, resolved at boot from the registry price table
    index: int         # agentic index — Ladder B must ascend STRICTLY on this


def _resolve_blend(model: str, declared: float | None) -> float | None:
    """Blend comes from the registry (live-refreshed for openrouter models,
    confirmed-static for DeepSeek). ESCALATION_POLICY §2.2 forbids hardcoding
    prices in routing.yaml, so a declared value is only a fallback for a model
    the registry does not carry."""
    from app.models_registry import registry

    info = registry.get(model)
    if info is not None:
        return info.blend
    return declared


def _load_quality(raw: dict) -> tuple[list[QualityRung], list[QualityRung], float, int]:
    """Partition rungs against auto_ceiling_blend by arithmetic, in BOTH
    directions: a rung whose live blend exceeds the ceiling moves to
    above_ceiling, and one at or under it is an automatic hop — wherever it was
    declared. Placement follows the price, never the position in the file."""
    spec = raw.get("quality_ladder", {}) or {}
    min_index = int(raw.get("min_index", 0))
    ceiling = float(spec.get("auto_ceiling_blend", 1.00))
    max_hops = int(spec.get("max_auto_hops", 2))

    declared: list[QualityRung] = []
    for group in (spec.get("rungs") or [], spec.get("above_ceiling") or []):
        for r in group:
            blend = _resolve_blend(r["model"], r.get("blend"))
            if blend is None:
                # No price anywhere: it cannot be PROVEN under the ceiling, so
                # it does not get to be an automatic hop.
                blend = float("inf")
            index = int(r.get("index") or 0)
            if index < min_index:
                raise IndexFloorError(
                    f"quality rung {r['model']} @ {r['provider']} has index {index}, "
                    f"below min_index {min_index}"
                )
            declared.append(
                QualityRung(model=r["model"], provider=r["provider"],
                            blend=blend, index=index)
            )

    auto = sorted([r for r in declared if r.blend <= ceiling], key=lambda r: (r.index, r.blend))
    above = sorted([r for r in declared if r.blend > ceiling], key=lambda r: (r.index, r.blend))

    # LADDER_V2 §2: quality escalation requires a STRICTLY increasing index.
    # Equal-index entries are lateral (same model on another provider) and are
    # fine; what must never happen is a later rung scoring lower than an
    # earlier one, which would sell a downgrade as an escalation.
    seen = [r.index for r in auto]
    if seen != sorted(seen):
        raise ValueError(f"quality ladder indices must be non-decreasing, got {seen}")
    return auto, above, ceiling, max_hops


_LADDER, _REMEDIATION = _load()
_QUALITY, _ABOVE_CEILING, _CEILING, _MAX_AUTO_HOPS = _load_quality(
    yaml.safe_load(ROUTING_YAML_PATH.read_text())
)


def quality_ladder() -> list[QualityRung]:
    """Automatic Ladder-B rungs, ascending blend. Never contains a rung above
    auto_ceiling_blend."""
    return list(_QUALITY)


def above_ceiling() -> list[QualityRung]:
    """Rungs over the ceiling — reachable only via explicit approval, never as
    an automatic hop."""
    return list(_ABOVE_CEILING)


def auto_ceiling_blend() -> float:
    return _CEILING


def max_auto_hops() -> int:
    return _MAX_AUTO_HOPS


def ladder(client_facing: bool = False) -> list[Rung]:
    """Ordered rungs. For client-facing paths, only client_ok rungs — a
    client request never silently degrades onto a free experimental rung."""
    if client_facing:
        return [r for r in _LADDER if r.client_ok]
    return list(_LADDER)


def remediation(result_code: str) -> str:
    return _REMEDIATION.get(result_code, "no remediation mapped for this result")


def reload() -> None:
    """Test hook — re-reads routing.yaml from ROUTING_YAML_PATH."""
    global _LADDER, _REMEDIATION, _QUALITY, _ABOVE_CEILING, _CEILING, _MAX_AUTO_HOPS
    # Read the module global at call time, not _load's def-time default, so a
    # test that monkeypatches ROUTING_YAML_PATH actually gets its own file.
    _LADDER, _REMEDIATION = _load(ROUTING_YAML_PATH)
    _QUALITY, _ABOVE_CEILING, _CEILING, _MAX_AUTO_HOPS = _load_quality(
        yaml.safe_load(ROUTING_YAML_PATH.read_text())
    )
