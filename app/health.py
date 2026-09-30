"""Preflight upstream health cache (ROUTING_RESILIENCE.md §1).

The point of this module is to stop wasting time (and log lines) attempting
upstreams that are known-dead. Routing consults `is_usable()` BEFORE dispatch;
an upstream marked unfunded/unauthorized/rate_limited is skipped, not tried.

State is PASSIVE (I7_MAIN §2.7): every real dispatch folds its result in via
record_result(). `probe_loop` only re-probes providers already marked dead
(unfunded/unauthorized/rate_limited), every 30 min, with a 1-token completion
— never a `/models` call, because `/models` succeeds on an unfunded account
and is exactly what hid the original outage. A healthy or unknown provider is
never probed: probes burn free-tier daily quota for nothing.
An empty completion (200 with zero content tokens) is the unfunded signature;
two in a row marks the upstream `unfunded`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.routing import ladder
from app.upstreams import call_provider, is_empty_completion

logger = logging.getLogger("llm-gate.health")

# states
LIVE = "live"
UNFUNDED = "unfunded"
UNAUTHORIZED = "unauthorized"
RATE_LIMITED = "rate_limited"
UNKNOWN = "unknown"

# An upstream in one of these states is skipped by routing before dispatch.
# `unknown` (never probed, or a transient hiccup) is NOT skipped — we don't
# know it's dead, so the ladder is allowed to find out.
_SKIP_STATES = {UNFUNDED, UNAUTHORIZED, RATE_LIMITED}

# result codes surfaced in the §4 failure envelope, keyed to routing.yaml
# remediation strings.
_STATE_TO_RESULT = {
    LIVE: "live",
    UNFUNDED: "unfunded",
    UNAUTHORIZED: "unauthorized",
    RATE_LIMITED: "rate_limited",
    UNKNOWN: "unknown",
}


@dataclass
class UpstreamHealth:
    status: str = UNKNOWN
    checked_at: float = 0.0
    detail: str = ""


_health: dict[str, UpstreamHealth] = {}
_empty_streak: dict[str, int] = {}


def _provider_key(provider: str) -> str:
    return {
        "deepseek": settings.deepseek_api_key,
        "openrouter": settings.openrouter_api_key,
        "aistudio": settings.google_ai_api_key,
        "groq": settings.groq_api_key,
        "zai": settings.zai_api_key,
        "cerebras": settings.cerebras_api_key,
    }.get(provider, "")


def _providers_and_probe_models() -> dict[str, str]:
    """Distinct providers in the ladder mapped to the model to probe them with
    (the lowest-rung model that uses that provider)."""
    out: dict[str, str] = {}
    for rung in ladder():
        out.setdefault(rung.provider, rung.model)
    return out


def _set(provider: str, status: str, detail: str) -> None:
    _health[provider] = UpstreamHealth(status=status, checked_at=time.time(), detail=detail)


def record_result(
    provider: str,
    status_code: int | None,
    body: dict | None = None,
    error: str | None = None,
) -> None:
    """Fold one observed upstream result into the cache. Called by the probe
    loop and by live dispatch, so real traffic keeps health fresh between
    probes."""
    if error is not None:
        _empty_streak[provider] = 0
        _set(provider, UNKNOWN, f"probe error: {error}")
        return
    if status_code in (401, 403):
        _empty_streak[provider] = 0
        _set(provider, UNAUTHORIZED, f"{status_code} unauthorized")
        return
    if status_code == 429:
        _empty_streak[provider] = 0
        _set(provider, RATE_LIMITED, "429 rate limited")
        return
    if status_code is not None and status_code >= 500:
        _empty_streak[provider] = 0
        _set(provider, UNKNOWN, f"{status_code} server error")
        return
    if status_code != 200:
        # 400/404/etc are request-level (bad model id, etc.), not an account
        # health signal — don't poison the cache over them.
        _empty_streak[provider] = 0
        _set(provider, UNKNOWN, f"{status_code}")
        return
    # 200 — the subtle case: empty completion == unfunded signature.
    if body is not None and is_empty_completion(body):
        streak = _empty_streak.get(provider, 0) + 1
        _empty_streak[provider] = streak
        if streak >= 2:
            _set(provider, UNFUNDED, "200 with 0 content tokens (2x)")
        else:
            # first empty — suspicious but not yet conclusive; stays usable
            _set(provider, UNKNOWN, "200 with 0 content tokens (1x)")
        return
    _empty_streak[provider] = 0
    _set(provider, LIVE, "ok")


def mark(provider: str, status: str, detail: str = "") -> None:
    """Explicit override (e.g. missing key -> unauthorized without a call)."""
    if status != UNFUNDED:
        _empty_streak[provider] = 0
    _set(provider, status, detail)


def get(provider: str) -> UpstreamHealth:
    return _health.get(provider, UpstreamHealth())


def configured(provider: str) -> bool:
    """True iff this provider has an API key set — an unconfigured provider is
    skipped by the ladder without a wasted network call."""
    return bool(_provider_key(provider))


def is_usable(provider: str) -> bool:
    """True unless the provider is in a known-dead state. An unprobed provider
    is usable (the ladder finds out); only unfunded/unauthorized/rate_limited
    are skipped."""
    return get(provider).status not in _SKIP_STATES


def result_code(provider: str) -> str:
    return _STATE_TO_RESULT.get(get(provider).status, "unknown")


def snapshot() -> dict[str, dict]:
    """The /admin/health table — the one command to run when something breaks."""
    out: dict[str, dict] = {}
    for provider in _providers_and_probe_models():
        h = get(provider)
        out[provider] = {
            "status": h.status,
            "checked_at": h.checked_at,
            "age_seconds": round(time.time() - h.checked_at, 1) if h.checked_at else None,
            "usable": is_usable(provider),
            "detail": h.detail,
        }
    return out


async def probe_once(client: httpx.AsyncClient) -> None:
    """One 1-token completion per DEAD upstream. Never /models, never a
    live/unknown provider (passive health covers those)."""
    for provider, model in _providers_and_probe_models().items():
        if not _provider_key(provider):
            mark(provider, UNAUTHORIZED, f"no {provider.upper()}_API_KEY configured")
            continue
        if get(provider).status not in _SKIP_STATES:
            continue
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        try:
            resp = await call_provider(client, provider, model, payload)
        except httpx.TimeoutException:
            record_result(provider, None, error="timeout")
            continue
        except httpx.HTTPError as exc:
            record_result(provider, None, error=str(exc))
            continue
        try:
            body = resp.json()
        except ValueError:
            body = None
        record_result(provider, resp.status_code, body)
    logger.info("health probe complete: %s", {p: h["status"] for p, h in snapshot().items()})


async def probe_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await probe_once(client)
            except Exception:
                logger.exception("health probe loop crashed, will retry next interval")
            await asyncio.sleep(settings.health_probe_interval_seconds)


def reset_for_test() -> None:
    """Test hook — clears cached health and empty streaks."""
    _health.clear()
    _empty_streak.clear()
