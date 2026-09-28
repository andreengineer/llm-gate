"""Tier classification + price table. blend = 0.75*in + 0.25*out (USD/M).

Routing rule (fixed, not price-dependent): model id prefixed "deepseek/" goes
to the DeepSeek direct upstream. Everything else goes to OpenRouter. Tier
classification is independent of routing and just gates policy (auto-allow /
approval / 403).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger("llm-gate.registry")

FREE = "free"
CHEAP = "cheap"
MID = "mid"
UI_ONLY = "ui_only"

# DeepSeek does not expose live per-model pricing over its API, so these stay
# static while every openrouter model is live-refreshed at boot.
#
# CONFIRMED 2026-08-04 against https://api-docs.deepseek.com/quick_start/pricing
# (they were unverified placeholders before, and both were wrong — v4-flash
# output was $0.60 vs a real $0.28, v4-pro was $0.50/$2.00 vs a real
# $0.435/$0.87, so the ledger was over-costing every DeepSeek call ~2.2x on
# output and tripping budget caps early).
#
# CAVEAT — these are OFF-PEAK list prices. DeepSeek doubles them during peak
# (09:00-12:00 and 14:00-18:00 Beijing / UTC+8). At peak, v4-pro blends to
# $1.087/M and crosses the $1.00 auto-escalation ceiling; off-peak it blends to
# $0.544/M and sits under it. Any rule that decides "automatic vs approval" by
# comparing blend to a fixed ceiling is therefore time-dependent for v4-pro —
# do not treat that comparison as a stable constant (see ESCALATION_POLICY.md
# §2.2, which assumed a fixed $2.18 blend that no source supports).
#
# "deepseek-chat" and "deepseek-reasoner" (DeepSeek's real-world API model
# names) are NOT valid in this environment — confirmed by replaying a real
# Hermes request directly against api.deepseek.com, which rejected
# "deepseek-chat" with: "The supported API model names are deepseek-v4-pro
# or deepseek-v4-flash". Only those two models are real and callable here.
# Both are cheap tier — DeepSeek has NO real mid-tier model in this
# environment; the earlier "deepseek-reasoner" mid-tier entry was an
# unverified placeholder invented before any real API test. Do not
# reintroduce a fake DeepSeek model id without testing it live first.
#
# PRICES CORRECTED 2026-09-18 (NG) against the official DeepSeek pricing page
# fetched 2026-09-17. Supersedes the 2026-08-04 values (0.14/0.28 flash,
# 0.435/0.87 pro), which under-stated flash output by 2.14x and pro output by
# 2.28x AND carried no peak awareness at all — so every peak-hour call was
# billed at off-peak rates and the whole $2.00/day cap was denominated in fake
# dollars (true ceiling ≈ 2x the believed one on peak-heavy days).
#
# Vendor page: peak = 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri, billed at 2x
# the off-peak rate. The values below are the OFF-PEAK base; price_at() applies
# DEEPSEEK_PEAK_MULTIPLIER when the call lands inside a peak window.
DEEPSEEK_STATIC_PRICES: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-v4-flash": (0.15, 0.60),   # $/M in(miss), out — off-peak blend 0.2625
    "deepseek/deepseek-v4-pro": (0.66, 1.98),     # $/M in(miss), out — off-peak blend 0.99
}
# Cache-HIT input price (I7_MAIN.md §2.2, confirmed 2026-09-26 against
# api-docs.deepseek.com). Real traffic Jun24-Jul24 was 99.4% cache hits;
# billing every input token at the miss rate above overstated cost ~38x and
# tripped budget caps on spend that never happened. Not a constant ratio to
# the miss price (50x for flash, 30x for pro) so it must be its own table,
# not derived. Kept separate from DEEPSEEK_STATIC_PRICES/SEED_ALLOWLIST on
# purpose: the registry/blend/tier/ceiling machinery only ever needs a single
# conservative (miss) input price, and reshaping that widely-consumed tuple
# to 3 values would ripple into boot-time price checks and tier classification
# for no benefit — only compute_cost() needs the hit price.
DEEPSEEK_CACHE_HIT_PRICES: dict[str, float] = {
    "deepseek/deepseek-v4-flash": 0.003,   # $/M — off-peak
    "deepseek/deepseek-v4-pro": 0.022,     # $/M — off-peak
}
DEEPSEEK_PEAK_MULTIPLIER = 2.0
DEEPSEEK_PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

# Chinese public holidays are fully off-peak (I7_MAIN.md §2.1). ISO dates,
# China Standard Time (the holiday is a calendar-day concept there, not UTC).
# 2026 National Day confirmed 2026-09-28 against the State Council General
# Office notice (issued 2025-11-04): Oct 1-7 inclusive. Add each year's block
# after checking the official notice — do not guess ahead of it.
DEEPSEEK_OFF_PEAK_HOLIDAYS_CST: frozenset[str] = frozenset({
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
})


def is_deepseek_peak(ts: float | None = None) -> bool:
    """True inside a DeepSeek peak window (01:00-04:00 / 06:00-10:00 UTC, Mon-Fri)."""
    now = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    if now.weekday() >= 5:          # Sat/Sun are off-peak all day
        return False
    cst = now.astimezone(timezone(timedelta(hours=8)))
    if cst.strftime("%Y-%m-%d") in DEEPSEEK_OFF_PEAK_HOLIDAYS_CST:
        return False
    return any(start <= now.hour < end for start, end in DEEPSEEK_PEAK_WINDOWS_UTC)


def price_at(model_id: str, ts: float | None = None) -> tuple[float, float, float] | None:
    """(input_miss_per_m, input_hit_per_m, output_per_m) in USD at time `ts`,
    peak-aware for DeepSeek. Single source of truth for billing: static
    DeepSeek rows get the peak multiplier (applied to hit and miss alike —
    confirmed 2x on both in the vendor table), everything else falls through
    to the live registry entry with hit==miss (no cache-hit discount modeled
    for non-DeepSeek upstreams).
    """
    base = DEEPSEEK_STATIC_PRICES.get(model_id)
    if base is None:
        info = registry.get(model_id)
        return None if info is None else (info.input_per_m, info.input_per_m, info.output_per_m)
    hit = DEEPSEEK_CACHE_HIT_PRICES.get(model_id, base[0])
    mult = DEEPSEEK_PEAK_MULTIPLIER if is_deepseek_peak(ts) else 1.0
    return (base[0] * mult, hit * mult, base[1] * mult)

# Callers that address DeepSeek models without the gate's "deepseek/" prefix
# (e.g. openclaw/hermes send the bare provider-catalog id, not gate-style
# routing keys) resolve here first. "deepseek-chat"/"deepseek-reasoner" are
# real-world DeepSeek names that don't exist in this environment (see note
# above) — mapped to their closest real equivalent rather than left to 400
# against the real upstream on every call.
BARE_MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-chat": "deepseek/deepseek-v4-flash",
    "deepseek-reasoner": "deepseek/deepseek-v4-pro",
}


def resolve_model_id(model_id: str) -> str:
    return BARE_MODEL_ALIASES.get(model_id, model_id)

# Seed allowlist. Confirmed against live OpenRouter pricing at boot; a model
# that drifts above its tier ceiling is auto-dropped, never silently promoted.
SEED_ALLOWLIST: dict[str, tuple[float, float]] = {
    **DEEPSEEK_STATIC_PRICES,
    "google/gemini-2.5-flash": (0.075, 0.30),
    "z-ai/glm-4.6-air": (0.10, 0.40),
    # LADDER_V2 §2.1 Ladder-A peer. Live OpenRouter price confirmed 2026-08-04:
    # $0.06 in / $0.40 out (the spec table said 0.10/0.30). Cheap input matters
    # here — on the gate's 0.75/0.25 weighting it blends to $0.145, UNDER
    # deepseek-v4-flash's $0.175, so it is a genuinely non-increasing failover
    # hop even though a naive (in+out)/2 average would call it more expensive.
    "z-ai/glm-4.7-flash": (0.06, 0.40),
    # gemini-2.5-pro blend = 3.4375, i.e. already over the 3.00 ui-only
    # ceiling on these list prices — this is intentional: it demonstrates the
    # "moves to ui-only automatically" rule from the boot-time price check.
    "google/gemini-2.5-pro": (1.25, 10.0),
    "z-ai/glm-5.2": (1.50, 3.50),
    # real mid-tier example (DeepSeek has none in this environment, see
    # above) — blend ~1.6, live-refreshed like everything else at boot
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
}


def blend(input_per_m: float, output_per_m: float) -> float:
    return round(0.75 * input_per_m + 0.25 * output_per_m, 6)


def classify(b: float) -> str:
    if b <= 0:
        return FREE
    if b <= settings.tier_cheap_max:
        return CHEAP
    if b <= settings.tier_mid_max:
        return MID
    return UI_ONLY


@dataclass
class ModelInfo:
    model_id: str
    input_per_m: float
    output_per_m: float
    upstream: str  # "deepseek" | "openrouter"

    @property
    def blend(self) -> float:
        return blend(self.input_per_m, self.output_per_m)

    @property
    def tier(self) -> str:
        return classify(self.blend)


def upstream_for(model_id: str) -> str:
    return "deepseek" if model_id.startswith("deepseek/") else "openrouter"


@dataclass
class ModelRegistry:
    models: dict[str, ModelInfo] = field(default_factory=dict)
    last_refresh: float = 0.0
    dropped: dict[str, str] = field(default_factory=dict)

    def seed(self) -> None:
        for model_id, (i, o) in SEED_ALLOWLIST.items():
            self.models[model_id] = ModelInfo(model_id, i, o, upstream_for(model_id))

    def get(self, model_id: str) -> ModelInfo | None:
        return self.models.get(resolve_model_id(model_id))

    def is_allowlisted(self, model_id: str) -> bool:
        canonical = resolve_model_id(model_id)
        return canonical in self.models and canonical not in self.dropped

    async def refresh_from_openrouter(self, client: httpx.AsyncClient) -> None:
        """Pull live pricing from OpenRouter for models we already carry, and
        discover ":free" variants of already-allowlisted cheap-tier models
        (free tier — see chat.py's task-class gate). A model whose live
        blend crosses out of the tier it was seeded at gets re-tiered
        automatically; nothing here changes DeepSeek prices (no live
        pricing endpoint) or adds any OTHER new model to the allowlist —
        only :free siblings of models already trusted as cheap."""
        try:
            resp = await client.get(
                f"{settings.openrouter_base_url}/models",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                timeout=10.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("price refresh failed, keeping seed/last-known prices: %s", exc)
            return

        live = {m["id"]: m for m in resp.json().get("data", [])}
        for model_id, info in list(self.models.items()):
            if info.upstream != "openrouter":
                continue
            row = live.get(model_id)
            if row is None:
                continue
            pricing = row.get("pricing", {})
            try:
                in_per_tok = float(pricing.get("prompt", 0))
                out_per_tok = float(pricing.get("completion", 0))
            except (TypeError, ValueError):
                continue
            new_in, new_out = in_per_tok * 1_000_000, out_per_tok * 1_000_000
            old_tier = info.tier
            info.input_per_m, info.output_per_m = new_in, new_out
            new_tier = info.tier
            if new_tier == UI_ONLY and old_tier != UI_ONLY:
                self.dropped[model_id] = (
                    f"drifted to blend ${info.blend}/M (was {old_tier}) at {time.time()}"
                )
                logger.error("AUTO-DROP %s: %s", model_id, self.dropped[model_id])
            elif old_tier != new_tier:
                logger.warning(
                    "RE-TIER %s: %s -> %s (blend $%.3f/M)", model_id, old_tier, new_tier, info.blend
                )

        # free-tier discovery: only for :free siblings of models this
        # registry already trusts as cheap tier — never a blanket import of
        # every :free model OpenRouter happens to list
        for model_id, info in list(self.models.items()):
            if info.upstream != "openrouter" or info.tier != CHEAP:
                continue
            free_id = f"{model_id}:free"
            if free_id in self.models or free_id not in live:
                continue
            self.models[free_id] = ModelInfo(free_id, 0.0, 0.0, "openrouter")
            logger.info("FREE-TIER discovered: %s (paid sibling %s is cheap)", free_id, model_id)

        self.last_refresh = time.time()

    def needs_refresh(self) -> bool:
        return (time.time() - self.last_refresh) > settings.price_refresh_interval_hours * 3600


registry = ModelRegistry()
registry.seed()
