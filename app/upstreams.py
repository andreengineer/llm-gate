"""Two upstream adapters, one gate. Real keys live only here.

Routing: model id prefixed "deepseek/" -> DeepSeek direct. Anything else ->
OpenRouter. The only automatic cross-provider hop is the fallback rule:
DeepSeek 5xx/timeout/429 -> exactly one retry on OpenRouter, cheap tier only,
never escalating tier on failure.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.models_registry import registry, CHEAP, FREE, price_at

logger = logging.getLogger("llm-gate.upstream")

# Fixed fallback target: must already be seeded as a cheap-tier OpenRouter
# model. Never picked dynamically — dynamic choice is exactly the kind of
# agent discretion this gate exists to remove.
FALLBACK_MODEL = "google/gemini-2.5-flash"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class UpstreamBadResponseError(RuntimeError):
    """A 2xx response with an empty/unparseable body — observed from real
    DeepSeek traffic under rapid retries with heavy prompt-cache hits. Not a
    gate bug to crash on; the upstream just didn't return usable JSON."""

    def __init__(self, upstream: str, status_code: int):
        self.upstream = upstream
        self.status_code = status_code
        super().__init__(f"{upstream} returned {status_code} with an empty/unparseable body")


def _parse_json_or_raise(resp: httpx.Response, upstream: str) -> dict:
    try:
        return resp.json()
    except ValueError as exc:
        logger.warning("%s returned an unparseable body (content-type=%s): %r",
                        upstream, resp.headers.get("content-type"), resp.content[:200])
        raise UpstreamBadResponseError(upstream, resp.status_code) from exc


@dataclass
class UpstreamResult:
    body: dict
    upstream_used: str
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    fallback_hop: bool = False
    free_fallback: bool = False
    cache_hit_tokens: int = 0


@dataclass
class RungAttempt:
    """One attempted rung, for the §4 no_live_route envelope."""
    rung: int
    provider: str
    model: str
    result: str   # unfunded | unauthorized | rate_limited | server_error | timeout | empty
    detail: str


class NoLiveRouteError(RuntimeError):
    """Every rung was skipped (known-dead) or attempted-and-failed. Carries the
    per-rung breakdown so chat.py can build the §4 503 envelope."""

    def __init__(self, attempted: list["RungAttempt"], skipped: list[dict]):
        self.attempted = attempted
        self.skipped = skipped
        super().__init__("no_live_route")


# Upstream statuses that mean "this provider can't serve right now, try the
# next rung" (ROUTING_RESILIENCE §3 outer loop). 402/400/404 are deliberately
# NOT here: a billing/bad-request error is surfaced to the caller, never masked
# behind a working-looking 200 from a different provider.
ESCALATE_STATUS = {401, 403, 408, 409, 429}


def _classify_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "unauthorized"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "unknown"


def _forced_params(payload: dict, upstream: str) -> dict:
    out = dict(payload)
    # Internal-only marker set by chat.py from the X-Think header; never sent
    # upstream. Popped unconditionally so it can't leak into any provider's
    # JSON body regardless of which branch below runs.
    think_requested = out.pop("_think_requested", False)
    out.setdefault("max_tokens", settings.default_max_tokens)
    # `usage: {include: true}` is an OpenRouter/DeepSeek extension. Google AI
    # Studio and Groq return usage natively and 400 on the unknown field, so
    # only the two upstreams that accept it get it.
    if upstream in ("openrouter", "deepseek"):
        out["usage"] = {"include": True}
    # Hard-forced regardless of caller: a streamed upstream response breaks
    # every JSON-parsing assumption this gate makes (budgets, cache, ledger
    # all key off one response body). Callers that want streaming get a
    # synthetic SSE stream built from the full response — see app/sse.py —
    # never a real passthrough stream.
    out["stream"] = False
    out.pop("stream_options", None)  # only valid when stream:true, which is
    # never sent upstream — callers whose agent frameworks stream internally
    # (hermes) include this alongside stream:true; left in place it pairs
    # with the forced stream:false above and upstream 400s on the combination
    if upstream == "openrouter":
        out["provider"] = {"sort": "price", "allow_fallbacks": False}
    # I7_MAIN.md §2.3: DeepSeek defaults to thinking ON (reasoning billed as
    # output at 2-6.6x the base output rate). Force it off unless the caller
    # already specified thinking/reasoning_effort explicitly, or opted in via
    # X-Think: 1 (in which case we leave the field unset and DeepSeek's own
    # default — thinking ON — applies).
    if upstream == "deepseek" and not think_requested \
            and "thinking" not in out and "reasoning_effort" not in out:
        out["thinking"] = {"type": "disabled"}
    out.pop("models", None)  # fallback arrays are handled by the gate, not upstream
    return out


def is_empty_completion(body: dict) -> bool:
    """A 200 response that produced no usable output — the signature that hid
    the DeepSeek outage (200 with zero content tokens). A response carrying
    real text, tool calls, or reasoning is NOT empty. Two of these in a row
    marks an upstream `unfunded` (see app/health.py)."""
    if not isinstance(body, dict):
        return True
    choices = body.get("choices") or []
    if not choices:
        return True
    for ch in choices:
        msg = (ch or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return False
        if isinstance(content, list) and content:
            return False
        if msg.get("tool_calls") or msg.get("reasoning_content") or msg.get("reasoning"):
            return False
    return True


async def _call_deepseek(client: httpx.AsyncClient, model_id: str, payload: dict) -> httpx.Response:
    body = _forced_params(payload, "deepseek")
    body["model"] = model_id.removeprefix("deepseek/")
    return await client.post(
        f"{settings.deepseek_base_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=60.0,
    )


async def _call_openrouter(client: httpx.AsyncClient, model_id: str, payload: dict) -> httpx.Response:
    body = _forced_params(payload, "openrouter")
    body["model"] = model_id
    return await client.post(
        f"{settings.openrouter_base_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=60.0,
    )


async def _call_aistudio(client: httpx.AsyncClient, model_id: str, payload: dict) -> httpx.Response:
    """Google AI Studio via its OpenAI-compatible endpoint (ROUTING_RESILIENCE
    rung 2). Independent account/key from OpenRouter — a distinct failure mode,
    which is the whole point of adding it."""
    body = _forced_params(payload, "aistudio")
    body["model"] = model_id
    return await client.post(
        f"{settings.aistudio_base_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {settings.google_ai_api_key}"},
        timeout=60.0,
    )


async def _call_groq(client: httpx.AsyncClient, model_id: str, payload: dict) -> httpx.Response:
    """Groq free developer tier (ROUTING_RESILIENCE rung 3) — lowest latency of
    the free paths, independent account from the others."""
    body = _forced_params(payload, "groq")
    body["model"] = model_id
    return await client.post(
        f"{settings.groq_base_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        timeout=60.0,
    )


# provider (health-cache key) -> adapter. deepseek's adapter strips the
# "deepseek/" routing prefix; the others take the model id as-is.
_PROVIDER_CALLERS = {
    "deepseek": _call_deepseek,
    "openrouter": _call_openrouter,
    "aistudio": _call_aistudio,
    "groq": _call_groq,
}


async def call_provider(
    client: httpx.AsyncClient, provider: str, model_id: str, payload: dict
) -> httpx.Response:
    """Dispatch one call to a named provider. Used by the escalation ladder
    (app/upstreams.dispatch) and the health probe (app/health.py)."""
    caller = _PROVIDER_CALLERS.get(provider)
    if caller is None:
        raise ValueError(f"unknown provider {provider!r}")
    return await caller(client, model_id, payload)


def extract_usage(body: dict) -> tuple[int, int, int]:
    usage = body.get("usage") or {}
    return (
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        int(usage.get("prompt_cache_hit_tokens", 0)),
    )


def compute_cost(
    model_id: str, prompt_tokens: int, completion_tokens: int, cache_hit_tokens: int = 0
) -> float:
    """Bill a call at the price in force *at this moment*.

    DeepSeek is peak-priced (2x during 01:00-04:00 / 06:00-10:00 UTC, Mon-Fri)
    and cache-split (hit tokens bill at ~1/30-1/50 the miss rate), so the
    price must be resolved per call, not per process start, and prompt_tokens
    must be split into hit/miss before pricing (I7_MAIN.md §2.2).
    """
    info = registry.get(model_id)
    if info is None:
        return 0.0
    miss_per_m, hit_per_m, out_per_m = price_at(model_id) or (
        info.input_per_m, info.input_per_m, info.output_per_m,
    )
    hit_tokens = min(max(cache_hit_tokens, 0), prompt_tokens)
    miss_tokens = prompt_tokens - hit_tokens
    return (
        (miss_tokens / 1_000_000) * miss_per_m
        + (hit_tokens / 1_000_000) * hit_per_m
        + (completion_tokens / 1_000_000) * out_per_m
    )


async def dispatch(
    client: httpx.AsyncClient, model_id: str, payload: dict, *, client_facing: bool = False
) -> UpstreamResult:
    """Provider escalation ladder (ROUTING_RESILIENCE.md §3).

    Try the requested model on its own upstream first (honoring caller intent;
    policy/tier were already gated in chat.py), then fall across the
    health-filtered ladder to the next LIVE provider on a provider-level
    failure — 401/403/429, 5xx, timeout, empty completion, or an unparseable
    2xx. A billing/bad-request error (402/400/404) is surfaced, never masked
    behind a working-looking 200 from another provider. Capped at
    settings.max_provider_hops distinct providers. When every rung is skipped
    (known-dead) or attempted-and-failed, raise NoLiveRouteError so chat.py can
    return the §4 no_live_route envelope.
    """
    from app import health              # lazy import: health imports this module
    from app.routing import ladder as _ladder

    info = registry.get(model_id)
    primary_provider = info.upstream if info else "openrouter"
    primary_is_free = info.tier == FREE if info else False

    # requested model first, then the ladder rungs as failover
    order: list[tuple[int, str, str, bool]] = [(0, primary_provider, model_id, primary_is_free)]
    for r in _ladder(client_facing):
        order.append((r.rung, r.provider, r.model, r.free))

    attempted: list[RungAttempt] = []
    skipped: list[dict] = []
    tried: set[str] = set()
    hops = 0

    for rung, provider, model, is_free in order:
        if provider in tried:
            # This rung shares a provider (health-cache key) with one already
            # attempted this chain — e.g. rung 5's paid OpenRouter sibling
            # after rung 4's :free model already failed. Record it so the §4
            # envelope still accounts for every rung, not just the first hit
            # per provider.
            skipped.append({"rung": rung, "provider": provider, "result": "already_attempted",
                            "detail": "this provider already attempted earlier in this chain"})
            continue
        if not health.configured(provider):
            skipped.append({"rung": rung, "provider": provider, "result": "unconfigured",
                            "detail": "no API key set"})
            continue
        if not health.is_usable(provider):
            skipped.append({"rung": rung, "provider": provider, "result": health.result_code(provider),
                            "detail": health.get(provider).detail})
            continue
        if hops >= settings.max_provider_hops:
            skipped.append({"rung": rung, "provider": provider, "result": "hop_cap",
                            "detail": "max_provider_hops reached"})
            continue
        tried.add(provider)
        hops += 1

        try:
            resp = await call_provider(client, provider, model, payload)
        except httpx.TimeoutException:
            attempted.append(RungAttempt(rung, provider, model, "timeout", "no response before timeout"))
            health.record_result(provider, None, error="timeout")
            continue

        status = resp.status_code
        if status in ESCALATE_STATUS or status >= 500:
            code = _classify_status(status)
            attempted.append(RungAttempt(rung, provider, model, code, f"HTTP {status}"))
            health.record_result(provider, status)
            logger.warning("%s failed (%s) for %s, escalating down the ladder", provider, status, model)
            continue
        if status != 200:
            # 402 no balance / 400 bad request / 404 unknown model — a real
            # client-side error, surfaced so it gets fixed rather than papered
            # over with a silent cross-provider hop.
            resp.raise_for_status()

        # 200 — but an empty or unparseable body is the unfunded signature (or a
        # transient DeepSeek hiccup); escalate rather than return nothing.
        try:
            body = resp.json()
        except ValueError:
            attempted.append(RungAttempt(rung, provider, model, "empty", "200 with unparseable body"))
            health.record_result(provider, status, None)
            logger.warning("%s returned 200 with an unparseable body for %s, escalating", provider, model)
            continue
        if is_empty_completion(body):
            attempted.append(RungAttempt(rung, provider, model, "unfunded", "200 with 0 content tokens"))
            health.record_result(provider, status, body)
            logger.warning("%s returned an empty completion for %s, escalating", provider, model)
            continue

        # success
        health.record_result(provider, status, body)
        pt, ct, cht = extract_usage(body)
        return UpstreamResult(
            body, provider, model, pt, ct,
            fallback_hop=(provider != primary_provider),
            # primary request was free-tier but this call landed on a
            # non-free rung: the free ride ended and this response cost
            # real money (see chat.py's free_fallback alert).
            free_fallback=(primary_is_free and not is_free),
            cache_hit_tokens=cht,
        )

    raise NoLiveRouteError(attempted, skipped)
