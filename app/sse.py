"""Synthetic SSE streaming for clients that always request stream:true.

LangChain-based agent frameworks (dcode, and likely others) force
stream:true internally regardless of end-user experience — their agent
middleware calls `_astream` unconditionally. The gate forces every upstream
call to be non-streaming (stream:false) so budget/cache/ledger logic stays
simple and auditable against a single response body; if the original
client asked for a stream, the one full completion is translated into a
minimal valid SSE stream here instead of forcing every client integration
to special-case a proxy that doesn't really stream.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator


# Assistant-message fields that must survive the non-streaming -> SSE
# translation. `tool_calls` is the critical one: a tool-calling agent turn has
# empty content and carries everything in tool_calls, so dropping it turns a
# valid tool call into an empty assistant message — the client sees nothing to
# act on, retries the identical body, and eventually trips loop detection.
_PASSTHROUGH_FIELDS = ("reasoning_content", "refusal", "function_call")


def _delta_from_message(message: dict) -> dict:
    delta: dict = {
        "role": message.get("role", "assistant"),
        "content": message.get("content") or "",
    }

    tool_calls = message.get("tool_calls")
    if tool_calls:
        # Streaming tool_calls carry an `index` so clients can assemble them
        # across chunks; the non-streaming body has none, so number them here.
        delta["tool_calls"] = [
            {**call, "index": call.get("index", i)} for i, call in enumerate(tool_calls)
        ]

    for field in _PASSTHROUGH_FIELDS:
        if message.get(field):
            delta[field] = message[field]

    return delta


def _chunk(body: dict, delta: dict, finish_reason: str | None, include_usage: bool = False) -> str:
    chunk = {
        "id": body.get("id"),
        "object": "chat.completion.chunk",
        "created": body.get("created"),
        "model": body.get("model"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if include_usage and "usage" in body:
        chunk["usage"] = body["usage"]
    return f"data: {json.dumps(chunk)}\n\n"


async def synthesize_sse_stream(body: dict) -> AsyncIterator[bytes]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    yield _chunk(body, _delta_from_message(message), None).encode()
    yield _chunk(body, {}, finish_reason, include_usage=True).encode()
    yield b"data: [DONE]\n\n"
