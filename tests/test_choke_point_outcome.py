"""A1: the choke point must WRITE.

`enforce_gate_invariants` (app/main.py) is the single point every gated request
crosses. Before this change it returned bare `JSONResponse` objects for the
halted / unknown_port / missing_header refusals and never stamped a terminal
outcome on dispatched calls, so the ledger could not distinguish "the gate
refused everything" from "nothing happened".

Live evidence, 2026-09-17 (before the fix):
    calls rows ......... 1856
    outcome IS NULL .... 1853   <- dispatched, never resolved
    rejections rows .... 3      <- the only refusals that happened to cross a
                                   route-level record_rejection() call
    escalations ........ 0

These tests pin the write path closed: every refusal leaves a `rejections` row
AND a `calls` row with a non-NULL outcome, and every successful dispatch is
stamped "ok" so "zero successful calls" is a queryable fact.
"""

import httpx
import pytest

from app import ledger
from app.halt import halt as _halt, unlock as _unlock
from app.main import app

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _completion_body(model: str = "deepseek/deepseek-v4-flash") -> dict:
    return {
        "id": "cmpl-choke",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


async def _client(port: int = 8787):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://gate:{port}"
    )


def _rows(sql: str, *args):
    with ledger._conn() as c:
        return c.execute(sql, args).fetchall()


@pytest.mark.asyncio
async def test_successful_dispatch_is_stamped_ok(mock_upstreams):
    """A 200 must resolve the calls row to 'ok', not leave it NULL forever."""
    mock_upstreams.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_completion_body()))

    async with await _client(8788) as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    run_id = resp.headers["X-Gate-Run-Id"]

    rows = _rows("SELECT outcome FROM calls WHERE run_id = ?", run_id)
    assert rows, "a dispatched call must leave a calls row"
    assert all(r[0] == "ok" for r in rows), f"outcome must be 'ok', got {rows}"

    # ...and the ledger can therefore answer the question that matters.
    assert _rows("SELECT count(*) FROM calls WHERE run_id = ? AND outcome = 'ok'", run_id)[0][0] == 1


@pytest.mark.asyncio
async def test_halted_refusal_is_persisted(mock_upstreams):
    """503 halted used to vanish. It must now land in rejections AND calls."""
    _halt()
    try:
        async with await _client(8788) as c:
            resp = await c.post(
                "/v1/chat/completions",
                json={"model": "deepseek/deepseek-v4-flash", "messages": []},
                headers={"X-Run-Id": "r_halt_choke"},
            )
    finally:
        _unlock()

    assert resp.status_code == 503
    assert _rows(
        "SELECT reason_code, status_code, agent FROM rejections WHERE run_id = 'r_halt_choke'"
    ) == [("halted", 503, "isaura")]  # 8788 -> isaura; must NOT be "unknown"
    assert _rows("SELECT outcome FROM calls WHERE run_id = 'r_halt_choke'") == [("rejected:halted",)]


@pytest.mark.asyncio
async def test_unknown_port_refusal_is_persisted(mock_upstreams):
    """400 unknown_port used to vanish. It must now land in the ledger."""
    async with await _client(8799) as c:  # 8799 is not in ports.yaml
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "deepseek/deepseek-v4-flash", "messages": []},
            headers={"X-Run-Id": "r_port_choke"},
        )

    assert resp.status_code == 400
    assert _rows("SELECT reason_code, status_code FROM rejections WHERE run_id = 'r_port_choke'") == [
        ("unknown_port", 400)
    ]
    assert _rows("SELECT outcome FROM calls WHERE run_id = 'r_port_choke'") == [
        ("rejected:unknown_port",)
    ]


@pytest.mark.asyncio
async def test_missing_run_id_refusal_is_persisted(mock_upstreams):
    """400 missing_header on the manual port must be persisted too."""
    async with await _client(8787) as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "deepseek/deepseek-v4-flash", "messages": []},
        )
    assert resp.status_code == 400
    assert _rows(
        "SELECT reason_code, status_code FROM rejections WHERE reason_code = 'missing_header'"
    ), "missing_header refusal left no rejections row"


@pytest.mark.asyncio
async def test_choke_point_stamp_does_not_double_count_route_refusal(mock_upstreams):
    """A route-level refusal is already recorded; the middleware stamp must be a no-op."""
    async with await _client(8787) as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "openrouter/auto", "messages": []},
            headers={"X-Run-Id": "r_dup_choke"},
        )
    assert resp.status_code == 403

    assert _rows("SELECT count(*) FROM rejections WHERE run_id = 'r_dup_choke'")[0][0] == 1
    outcomes = _rows("SELECT DISTINCT outcome FROM calls WHERE run_id = 'r_dup_choke'")
    assert len(outcomes) == 1 and outcomes[0][0].startswith("rejected:"), outcomes


def test_stamp_outcome_only_touches_unresolved_rows():
    """stamp_outcome is NULL-only: it must never rewrite an existing verdict."""
    ledger.record_call(
        run_id="r_stamp_unit",
        agent="unit",
        model="m",
        upstream="u",
        tier="cheap",
        cost_usd=0.0,
        p_hash="h",
    )
    assert ledger.stamp_outcome("r_stamp_unit", "ok") == 1
    # second stamp finds nothing unresolved -> no-op, no clobber
    assert ledger.stamp_outcome("r_stamp_unit", "rejected:http_500") == 0
    assert _rows("SELECT outcome FROM calls WHERE run_id = 'r_stamp_unit'") == [("ok",)]
