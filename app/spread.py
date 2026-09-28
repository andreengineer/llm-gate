"""Spread fan-out: pre-priced, never agent-discretionary.

Members come from spread_registry.json (owned by the buying-agent repo, not
this service). The gate resolves members, prices the whole spread against
remaining budget BEFORE dispatching slot 1, and asks for at most one itemized
approval for the whole spread — never one per slot.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx

from app.config import SPREAD_REGISTRY_PATH, settings
from app.models_registry import blend, classify, CHEAP
from app.ledger import global_daily_spend, mid_tier_daily_spend, record_call, prompt_hash
from app.telegram import request_mid_tier_approval
from app.upstreams import UpstreamBadResponseError, dispatch as upstream_dispatch

# Rough per-slot token estimate used only for pre-pricing a spread before any
# call happens. Real spend is still metered per-call by the ledger afterward.
EST_INPUT_TOKENS = 2000
EST_OUTPUT_TOKENS = 800


@dataclass
class SpreadMember:
    rank: int
    name: str
    role: str
    access: str
    api_available: bool
    cost_tier: str
    cost_in: float
    cost_out: float
    env_key: str | None
    model_id: str | None  # None for browser_ui-only members

    @property
    def is_sunk(self) -> bool:
        return self.cost_tier == "sunk" or not self.api_available

    @property
    def tier(self) -> str:
        if self.is_sunk:
            return CHEAP
        return classify(blend(self.cost_in, self.cost_out))

    @property
    def est_cost(self) -> float:
        if self.is_sunk:
            return 0.0
        return (EST_INPUT_TOKENS / 1_000_000) * self.cost_in + (EST_OUTPUT_TOKENS / 1_000_000) * self.cost_out


# Maps spread_registry.json human model names to gate-routable model ids.
# Only entries present here can actually be dispatched through the gate;
# anything else is treated as sunk/manual (browser handoff).
NAME_TO_MODEL_ID = {
    # "deepseek-reasoner"/"deepseek-chat" aren't real models in this
    # environment (see models_registry.py) — mapped to their closest real
    # DeepSeek equivalent, same as the bare-name alias table.
    "DeepSeek R1": "deepseek/deepseek-v4-pro",
    "DeepSeek V3.2": "deepseek/deepseek-v4-flash",
    "Gemini 2.5 Pro": "google/gemini-2.5-pro",
}


def load_members() -> list[SpreadMember]:
    data = json.loads(SPREAD_REGISTRY_PATH.read_text())
    members = []
    for m in data["models"]:
        members.append(
            SpreadMember(
                rank=m["rank"],
                name=m["name"],
                role=m["role"],
                access=m["access"],
                api_available=m["api_available"],
                cost_tier=m["cost_tier"],
                cost_in=m.get("cost_per_1m_in_usd") or 0.0,
                cost_out=m.get("cost_per_1m_out_usd") or 0.0,
                env_key=m.get("env_key"),
                model_id=NAME_TO_MODEL_ID.get(m["name"]),
            )
        )
    return members


@dataclass
class SpreadDecision:
    allowed: bool
    reason: str = ""
    status_code: int = 200
    error_code: str = ""
    members: list[SpreadMember] = field(default_factory=list)
    total_est_cost: float = 0.0


def resolve(mode: str, depth: int) -> SpreadDecision:
    if depth > settings.spread_max_depth:
        return SpreadDecision(False, f"depth {depth} exceeds max {settings.spread_max_depth}", 400, "depth_exceeded")

    mode_key = "l" if mode == "l" else "p"
    all_members = sorted(load_members(), key=lambda m: m.rank)
    compat = [m for m in all_members if mode_key in m.role or True]  # registry doesn't gate strictly by mode; rank order is authoritative
    members = sorted(all_members, key=lambda m: m.rank)[:depth]

    total_est = sum(m.est_cost for m in members)
    return SpreadDecision(True, members=members, total_est_cost=total_est)


async def check_budget_for_spread(decision: SpreadDecision) -> SpreadDecision:
    remaining = settings.global_daily_hard - global_daily_spend()
    if decision.total_est_cost > remaining:
        return SpreadDecision(
            False,
            f"spread est ${decision.total_est_cost:.4f} exceeds remaining daily budget ${remaining:.4f}",
            429,
            "spread_budget_rejected",
            members=decision.members,
            total_est_cost=decision.total_est_cost,
        )
    mid_members = [m for m in decision.members if m.tier == "mid"]
    if mid_members:
        remaining_mid = settings.mid_tier_daily_cap - mid_tier_daily_spend()
        mid_est = sum(m.est_cost for m in mid_members)
        if mid_est > remaining_mid:
            return SpreadDecision(
                False,
                f"spread mid-tier est ${mid_est:.4f} exceeds remaining mid budget ${remaining_mid:.4f}",
                429,
                "spread_budget_rejected",
                members=decision.members,
                total_est_cost=decision.total_est_cost,
            )
    return decision


async def approve_spread_if_needed(
    client: httpx.AsyncClient, agent: str, run_id: str, decision: SpreadDecision
) -> bool:
    needs_approval = any(m.tier != CHEAP and not m.is_sunk for m in decision.members)
    if not needs_approval:
        return True
    itemized = "\n".join(
        f"  slot {m.rank}: {m.name} ({m.tier}) est ${m.est_cost:.4f}" for m in decision.members
    )
    preview = f"spread depth={len(decision.members)}\n{itemized}"
    worst = max((m for m in decision.members if not m.is_sunk), key=lambda m: blend(m.cost_in, m.cost_out))
    return await request_mid_tier_approval(
        client, agent, run_id, worst.model_id or worst.name,
        decision.total_est_cost, mid_tier_daily_spend(), global_daily_spend(), preview,
    )


async def dispatch_spread(
    client: httpx.AsyncClient, agent: str, run_id: str, task: str, decision: SpreadDecision
) -> list[dict]:
    async def run_member(m: SpreadMember) -> dict:
        if m.is_sunk or not m.model_id:
            return {
                "rank": m.rank, "model": m.name, "access": m.access, "status": "manual_handoff",
                "instruction": f"Route to {m.access} ({m.access}); gate cannot call sunk-cost UI models.",
            }
        payload = {"messages": [{"role": "user", "content": task}]}
        try:
            result = await upstream_dispatch(client, m.model_id, payload)
        except (httpx.HTTPError, UpstreamBadResponseError) as exc:
            return {"rank": m.rank, "model": m.name, "status": "error", "detail": str(exc)}
        from app.upstreams import compute_cost
        cost = compute_cost(result.model_used, result.prompt_tokens, result.completion_tokens,
                            cache_hit_tokens=result.cache_hit_tokens)
        p_hash = prompt_hash(result.model_used, payload["messages"])
        record_call(run_id, agent, result.model_used, result.upstream_used, m.tier, cost, p_hash,
                    fallback_hop=result.fallback_hop)
        return {"rank": m.rank, "model": m.name, "status": "ok", "cost_usd": cost, "body": result.body}

    return list(await asyncio.gather(*(run_member(m) for m in decision.members)))
