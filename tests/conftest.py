import json
import os
import tempfile
from pathlib import Path

# Must happen before any `import app.*` anywhere (including via other test
# files pytest collects) — config.py reads these at import time.
_state_dir = tempfile.mkdtemp(prefix="llm-gate-test-state-")
os.environ["LLM_GATE_STATE_DIR"] = _state_dir
os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"
os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
# Explicitly blank rungs 2/3 rather than leaving them unset: config.py loads
# `.env` from cwd (app/config.py Settings(env_file=".env")), and the real
# ~/llm-gate/.env now carries a live GOOGLE_AI_API_KEY (added 2026-07-26) —
# without this override the test suite would silently pick up production
# config and treat aistudio as configured, dispatching real (unmocked) calls.
os.environ["GOOGLE_AI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
# Same leak, different shape (found 2026-09-17): agent/cron shells export
# OPENROUTER_BASE_URL=http://127.0.0.1:8789/v1 (the gate's own port, per
# scripts/verify_migration.sh). Inherited here it retargets every openrouter
# call at the gate itself, so respx mocks miss and 12 tests fail with
# "not mocked" — a false alarm that reads as a code regression. Pin the
# public base URLs so the suite is deterministic under any ambient env.
os.environ["OPENROUTER_BASE_URL"] = "https://openrouter.ai/api/v1"
os.environ["DEEPSEEK_BASE_URL"] = "https://api.deepseek.com"
os.environ["TELEGRAM_APPROVAL_BOT_TOKEN"] = "test-bot-token"
os.environ["TELEGRAM_APPROVAL_CHAT_ID"] = "12345"
os.environ["APPROVAL_TIMEOUT_SECONDS"] = "1"

_registry_path = Path(_state_dir) / "spread_registry.json"
_registry_path.write_text(json.dumps({
    "version": "1.0",
    "models": [
        {
            "rank": 1, "name": "Sunk UI", "role": "chairman_only", "access": "browser_ui",
            "api_available": False, "cost_tier": "sunk",
            "cost_per_1m_in_usd": 0, "cost_per_1m_out_usd": 0, "env_key": None,
        },
        {
            "rank": 2, "name": "DeepSeek V3.2", "role": "fast_generalist", "access": "api",
            "api_available": True, "cost_tier": "low",
            "cost_per_1m_in_usd": 0.27, "cost_per_1m_out_usd": 0.41, "env_key": "DEEPSEEK_KEY",
        },
        {
            "rank": 3, "name": "DeepSeek R1", "role": "reasoner", "access": "api",
            "api_available": True, "cost_tier": "medium",
            "cost_per_1m_in_usd": 1.00, "cost_per_1m_out_usd": 4.20, "env_key": "DEEPSEEK_KEY",
        },
        {
            "rank": 4, "name": "Gemini 2.5 Pro", "role": "vision_and_deep", "access": "api",
            "api_available": True, "cost_tier": "high",
            "cost_per_1m_in_usd": 1.25, "cost_per_1m_out_usd": 10.0, "env_key": "GEMINI_KEY",
        },
    ],
}))
os.environ["SPREAD_REGISTRY_PATH"] = str(_registry_path)

_ports_path = Path(_state_dir) / "ports.yaml"
_ports_path.write_text("""
8787: { agent_id: "manual",   require_run_id: true,  enforcement: "enforce"  }
8788: { agent_id: "isaura",   require_run_id: false, enforcement: "log_only" }
8789: { agent_id: "hermes",   require_run_id: false, enforcement: "log_only" }
8791: { agent_id: "dcode",    require_run_id: false, enforcement: "enforce"  }
8792: { agent_id: "opencode", require_run_id: false, enforcement: "enforce", default_task_class: "experiment" }
""")
os.environ["PORTS_YAML_PATH"] = str(_ports_path)

import pytest
import respx

from app.halt import unlock as _unlock
from app.ledger import _conn as _ledger_conn
from app.cache import _conn as _cache_conn


def _reset_dbs():
    with _ledger_conn() as c:
        c.execute("DELETE FROM calls")
        c.execute("DELETE FROM surges")
    with _cache_conn() as c:
        c.execute("DELETE FROM exact_cache")
    import app.telegram as telegram_mod
    telegram_mod._temp_approved.clear()
    telegram_mod._last_update_id = 0
    telegram_mod._reset_no_live_route_dedup_for_test()
    # health cache is process-global (updated by live dispatch), so it would
    # otherwise leak state across tests — reset it like the DBs.
    import app.health as health_mod
    health_mod.reset_for_test()


@pytest.fixture(autouse=True)
def _clean_state():
    _unlock()
    _reset_dbs()
    yield
    _unlock()
    _reset_dbs()


@pytest.fixture
def mock_upstreams():
    with respx.mock(assert_all_called=False) as router:
        yield router
