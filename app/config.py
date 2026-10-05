"""Settings for llm-gate. All provider keys live here ONLY — agents never see them."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

STATE_DIR = Path(os.environ.get("LLM_GATE_STATE_DIR", Path.home() / ".llm-gate"))
HALT_FILE = STATE_DIR / "HALT"
LEDGER_DB = STATE_DIR / "spend.db"
CACHE_DB = STATE_DIR / "cache.db"
SPREAD_REGISTRY_PATH = Path(
    os.environ.get(
        "SPREAD_REGISTRY_PATH",
        "/home/a/buying-agent/manifests/spread_registry.json",
    )
)
PORTS_YAML_PATH = Path(
    os.environ.get("PORTS_YAML_PATH", str(Path(__file__).resolve().parent.parent / "ports.yaml"))
)
ROUTING_YAML_PATH = Path(
    os.environ.get("ROUTING_YAML_PATH", str(Path(__file__).resolve().parent.parent / "routing.yaml"))
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # provider keys — only place these live
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""
    google_ai_api_key: str = ""   # Google AI Studio (Gemini) — ROUTING_RESILIENCE rung 2
    groq_api_key: str = ""        # Groq — dormant since I7_MAIN §2.5 dropped it from the ladder
    zai_api_key: str = ""         # Z.ai direct, GLM-4.7-Flash free (I7_MAIN §2.5 rung 3)
    cerebras_api_key: str = ""    # Cerebras free, 1M tok/day, 5 RPM (I7_MAIN §2.5 rung 4)

    deepseek_base_url: str = "https://api.deepseek.com"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Google's OpenAI-compatible endpoint — same request/response shape as the
    # other upstreams so one adapter path handles it (native Gemini API differs).
    aistudio_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    zai_base_url: str = "https://api.z.ai/api/paas/v4"
    cerebras_base_url: str = "https://api.cerebras.ai/v1"

    # telegram — approval channel MUST be a separate bot from the Hermes gateway bot
    telegram_approval_bot_token: str = ""
    telegram_approval_chat_id: str = ""

    # tiers (USD/M, blend = 0.75*in + 0.25*out)
    tier_cheap_max: float = 1.50
    tier_mid_max: float = 3.00

    # budgets (USD)
    global_daily_soft: float = 1.50
    global_daily_hard: float = 3.00  # raised 2026-09-28 from $2.00 (I7_MAIN.md
                                      # §2.4 asked for $3 hard / $1.50 soft-flag
                                      # headroom); hourly caps below are KEPT, not
                                      # removed, per user decision — they fixed a
                                      # real Sept-17 starvation incident and this
                                      # daily raise is additive, not a replacement
    mid_tier_daily_cap: float = 0.60
    per_run_id_max_calls: int = 100
    per_run_id_max_usd: float = 1.00     # raised 2026-09-17 from $0.25 — real DeepSeek blend is
                                          # ~$0.175/M (deepseek-real-prices memory); the old cap
                                          # left no headroom for one normal multi-turn conversation
                                          # and was starving legitimate runs into max_iterations
                                          # instead of catching real loops (see run 20260917_160312)
    per_agent_hourly_usd: float = 1.00   # raised 2026-09-17 from $0.40, same reason — still well
                                          # under global_daily_hard so one hour can't exhaust a day
    review_agent_hourly_usd: float = 0.40  # dedicated ceiling for the isolated "hermes-review"
                                            # bucket (ports.yaml :8793) — background skill/memory
                                            # curation should rarely need much; bounds a genuinely
                                            # runaway review loop without ever touching hermes's
                                            # own bucket (see agent/background_review.py)

    # loop detection
    repeat_hash_window_minutes: int = 10
    repeat_hash_max_count: int = 5

    # routing resilience (ROUTING_RESILIENCE.md)
    health_probe_interval_seconds: int = 1800  # I7_MAIN §2.7: re-probe DEAD providers only, every 30 min
    chain_window_seconds: int = 120            # retries of same body within this window = ONE attempt chain
    chain_max_attempts: int = 6                # >this many attempts in one chain = a real loop -> 429
    routing_yaml_path: str = ""                # override; default is app/../routing.yaml
    max_provider_hops: int = 3                 # outer-loop cap: distinct providers tried per chain
    escalation_daily_max: int = 15             # ESCALATION_POLICY §2.3 — Ladder-B hops/day, counted
                                               # separately from dollar budgets: escalation FREQUENCY
                                               # is a product signal, not just a cost event
    no_live_route_alert_dedup_minutes: int = 30

    # kill switch (2026-09-28 incident, gap #4: forgot-HALT watchdog)
    halt_stale_threshold_hours: int = 6            # HALT older than this == forgotten -> alert
    halt_watchdog_interval_seconds: int = 1800     # check the HALT file every 30 min
    halt_watchdog_alert_dedup_minutes: int = 360   # re-alert at most every 6h while still stale

    # approval
    approval_timeout_seconds: int = 90

    # fallback
    fallback_alert_threshold_per_day: int = 10

    # spread
    spread_max_depth: int = 4

    # cache
    exact_cache_ttl_hours: int = 24

    # misc
    default_max_tokens: int = 1500
    price_refresh_interval_hours: int = 6
    host: str = "127.0.0.1"
    port: int = 8787
    # enforcement mode is per-port now (ports.yaml), not global — see app/ports.py


settings = Settings()
STATE_DIR.mkdir(parents=True, exist_ok=True)
