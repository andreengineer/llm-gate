from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.ledger import record_would_block
from app.spread import approve_spread_if_needed, check_budget_for_spread, dispatch_spread, resolve

logger = logging.getLogger("llm-gate.spread")
router = APIRouter()


@router.post("/v1/spread")
async def spread(request: Request):
    x_agent_id = request.state.agent_id
    x_run_id = request.state.run_id
    log_only = request.state.enforcement == "log_only"
    payload = await request.json()
    mode = payload.get("mode")
    depth = payload.get("depth")
    task = payload.get("task")
    if mode not in ("l", "p") or not isinstance(depth, int) or not task:
        raise HTTPException(400, "spread requires {mode: 'l'|'p', depth: int, task: str}")

    # depth cap is a standing, categorical constraint — never relaxed, even log_only
    decision = resolve(mode, depth)
    if not decision.allowed:
        raise HTTPException(decision.status_code, detail={"error": decision.error_code, "reason": decision.reason})

    decision = await check_budget_for_spread(decision)
    if not decision.allowed:
        if log_only:
            record_would_block(x_run_id, x_agent_id, decision.error_code, decision.reason)
            logger.warning("[LOG-ONLY %s] spread would block (%s): %s", x_agent_id, decision.error_code, decision.reason)
        else:
            raise HTTPException(decision.status_code, detail={"error": decision.error_code, "reason": decision.reason})

    async with httpx.AsyncClient() as client:
        if log_only:
            needs_approval = any(m.tier != "cheap" and not m.is_sunk for m in decision.members)
            if needs_approval:
                record_would_block(x_run_id, x_agent_id, "spread_would_require_approval",
                                    f"members={len(decision.members)} est=${decision.total_est_cost:.4f}")
                logger.warning("[LOG-ONLY %s] spread would require approval: %d members est $%.4f",
                               x_agent_id, len(decision.members), decision.total_est_cost)
        else:
            approved = await approve_spread_if_needed(client, x_agent_id, x_run_id, decision)
            if not approved:
                raise HTTPException(429, detail={"error": "approval_timeout", "reason": "spread approval timed out or denied"})

        results = await dispatch_spread(client, x_agent_id, x_run_id, task, decision)

    return {"run_id": x_run_id, "members": len(decision.members), "results": results}
