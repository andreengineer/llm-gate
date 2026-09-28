from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.halt import is_halted
from app.identity import synthesize_run_id
from app.ledger import record_rejection, stamp_outcome
from app.models_registry import registry
from app.ports import all_ports, identity_for_port
from app.routes import admin, chat, spread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-gate")


def _record_choke_refusal(
    run_id: str | None,
    agent_id: str | None,
    error: str,
    status_code: int,
    reason: str,
    *,
    path: str = "",
) -> JSONResponse:
    """Persist an in-middleware refusal, then hand back the response.

    This middleware is the single choke point every gated request crosses, so a
    refusal that skips the ledger here is invisible forever: the gate can 503 on
    a halt for days and `rejections` still reads zero. Every refusal goes
    through `record_rejection()` (a `rejections` row *and* a `calls` row with a
    non-NULL outcome) before it goes out on the wire.

    Observability must never be able to break the refusal itself, hence the
    swallow — but it is logged loudly when it fails.
    """
    try:
        record_rejection(
            run_id=run_id or f"refused:{path or 'middleware'}",
            agent=agent_id or "unknown",
            reason_code=error,
            status_code=status_code,
            reason=reason,
        )
    except Exception:
        logger.exception("failed to persist choke-point refusal error=%s run_id=%s", error, run_id)
    return JSONResponse(status_code=status_code, content={"error": error, "reason": reason})


async def _refresh_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await registry.refresh_from_openrouter(client)
            except Exception:
                logger.exception("price refresh loop crashed, will retry next interval")
            await asyncio.sleep(settings.price_refresh_interval_hours * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app import health
    per_port = ", ".join(
        f"{p}={identity_for_port(p).agent_id}:{identity_for_port(p).enforcement}" for p in all_ports()
    )
    logger.info("llm-gate starting on %s — %s", settings.host, per_port)
    task = asyncio.create_task(_refresh_loop())  # first iteration refreshes immediately
    # ROUTING_RESILIENCE §1: probe upstream health on boot and every 15 min so
    # routing skips dead upstreams before dispatch instead of discovering them
    # one wasted request at a time.
    health_task = asyncio.create_task(health.probe_loop())
    yield
    task.cancel()
    health_task.cancel()


app = FastAPI(title="llm-gate", version="1.0", lifespan=lifespan)

GATED_PATHS = {"/v1/chat/completions", "/v1/spread"}
HALT_EXEMPT_PATHS = {"/health", "/admin/halt", "/admin/unlock"}
VALID_TASK_CLASSES = {"background", "experiment", "cron"}


@app.middleware("http")
async def enforce_gate_invariants(request: Request, call_next):
    path = request.url.path

    if is_halted() and path not in HALT_EXEMPT_PATHS:
        # The halt check runs before identity resolution, so the port still has
        # to be resolved here or every halt refusal lands in the ledger as
        # agent "unknown" — i.e. exactly the event where per-agent attribution
        # matters most becomes unqueryable. Best-effort: an unresolvable port
        # still degrades to "unknown" rather than raising.
        _halt_server = request.scope.get("server")
        _halt_agent = None
        if _halt_server:
            _halt_ident = identity_for_port(_halt_server[1])
            _halt_agent = _halt_ident.agent_id if _halt_ident else None
        return _record_choke_refusal(
            request.headers.get("x-run-id"),
            _halt_agent,
            "halted",
            503,
            "~/.llm-gate/HALT present",
            path=path,
        )

    if path in GATED_PATHS:
        server = request.scope.get("server")
        port = server[1] if server else settings.port
        identity = identity_for_port(port)
        if identity is None:
            return _record_choke_refusal(
                request.headers.get("x-run-id"),
                None,
                "unknown_port",
                400,
                f"port {port} has no identity in ports.yaml",
                path=path,
            )

        run_id = request.headers.get("x-run-id")
        if identity.require_run_id and not run_id:
            return _record_choke_refusal(
                run_id,
                identity.agent_id,
                "missing_header",
                400,
                "X-Run-Id is mandatory on this port",
                path=path,
            )
        if not run_id:
            body = await request.body()
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            run_id = synthesize_run_id(identity.agent_id, payload.get("messages", []))

        header_task_class = request.headers.get("x-task-class")
        task_class = header_task_class if header_task_class in VALID_TASK_CLASSES else identity.default_task_class

        request.state.agent_id = identity.agent_id
        request.state.run_id = run_id
        request.state.enforcement = identity.enforcement
        request.state.task_class = task_class

    response = await call_next(request)

    if path in GATED_PATHS and hasattr(request.state, "agent_id"):
        response.headers["X-Gate-Agent-Id"] = request.state.agent_id
        response.headers["X-Gate-Run-Id"] = request.state.run_id
        response.headers["X-Gate-Enforcement"] = request.state.enforcement

        # A1 choke-point outcome stamp. record_call() writes the spend row before
        # the terminal status is known, so without this every dispatched request
        # stays outcome=NULL and "zero successful calls" is unqueryable — the
        # blind spot that hid the outage. Refusals already persisted by
        # record_rejection() carry a non-NULL outcome and are left untouched.
        outcome = "ok" if response.status_code < 400 else f"rejected:http_{response.status_code}"
        try:
            stamp_outcome(request.state.run_id, outcome)
        except Exception:
            logger.exception("choke-point outcome stamp failed run_id=%s", request.state.run_id)

    return response


app.include_router(chat.router)
app.include_router(spread.router)
app.include_router(admin.router)
