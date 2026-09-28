# Multi-agent assistant stack — architecture snapshot

Generated 2026-07-25, for handoff into a new session (Opus). This is a
description of what's actually running today — verified against live
config, running services, and code, not a design doc. If you're picking up
work here, read this first, then `MIGRATION.md` (llm-gate's own build log)
for the blow-by-blow of how it got this way.

## The shape of it

Four agents. One proxy in front of three of them. Each agent has a
distinct job; none of them overlap in purpose.

```
                         ┌─────────────┐
  isaura (openclaw) ───► │             │
  :8788                  │             │
                         │   llm-gate   │──► DeepSeek API (direct)
  hermes (gateway) ───►  │  (FastAPI,   │
  :8789                  │  127.0.0.1)  │──► OpenRouter (everything else)
                         │             │
  dcode (deepagents) ──► │             │
  :8791                  └─────────────┘

  opencode ──────────────────────────────► OpenRouter (own key, direct,
  (Go CLI, own binary)                      NOT gate-mediated — see below)
```

- **isaura** — OpenClaw-based personal assistant/gateway. Chat-facing,
  procurement/search/agentic-messaging duties.
- **hermes** — long-running Python agent gateway (`~/.hermes/hermes-agent`,
  ~100+ modules), messaging-platform integration, "first receiver" for
  incoming work. Runs as two systemd services (see below).
- **dcode** — `deepagents-code`, LangChain-based, build/refactor/multi-file
  coding agent. Launched per-task by `bin/run-agent`, not always-on.
- **opencode** — separate Go CLI (`~/.opencode`), low-latency free-tier
  model testing for cron/night goals. Deliberately **not** routed through
  the gate (see "Why opencode is outside the gate" below).
- **claw** — not a separate system. Investigated and confirmed to be a role
  label for isaura's own gateway; its old Playwright duties live inside
  isaura's code or moved to Hermes's Browserbase integration. Port 8790 is
  reserved-but-unused in `ports.yaml` for this reason, not a live agent.

## llm-gate: what it actually is

`/home/a/llm-gate` — FastAPI + httpx + SQLite. A spend-guardrail proxy that
sits between agents and their LLM providers. Not a router for capability
reasons (it doesn't pick better models) — purely a cost/safety gate:
enforce budgets, kill runaway loops, deny known-bad model patterns, cache
duplicate calls, and require human approval for the DeepSeek→"real" jump
(mid-tier and up).

```
app/
  main.py          FastAPI app, lifespan (price-refresh loop), the one
                    middleware that resolves port→identity, run_id
                    (header or synthesized), HALT check, task_class
  config.py        Settings (pydantic-settings, reads .env). Provider keys
                    live ONLY here — agents never see them.
  ports.py         Port-as-identity: ports.yaml → {agent_id, require_run_id,
                    enforcement, default_task_class} per listener
  identity.py      synthesize_run_id() — sha8(first system prompt) + hour
                    bucket, for agents that can't set X-Run-Id themselves
  models_registry.py  Tier classification (blend = 0.75*in + 0.25*out
                    USD/M), DeepSeek static prices + bare-name aliasing,
                    OpenRouter live price refresh (every 6h)
  deny_list.py      Hard, categorical, never-relaxed: openrouter/auto,
                    perplexity/sonar*, *:online, *:extended
  upstreams.py      dispatch() — deepseek/ prefix → DeepSeek direct,
                    everything else → OpenRouter. _forced_params() hard-
                    forces stream:false + strips stream_options/models on
                    every outgoing call. One-shot fallback to a fixed cheap
                    OpenRouter model on 5xx/timeout/empty-body.
  sse.py            Synthesizes a minimal SSE stream back to callers that
                    asked for stream:true, from the one full JSON response
                    the gate actually gets upstream
  ledger.py         SQLite (~/.llm-gate/spend.db): calls, would_block_events,
                    surges tables. Budget/loop/dedup checks.
  cache.py          Exact-match response cache (24h TTL), list-content-aware
                    message normalization
  halt.py           ~/.llm-gate/HALT file presence = full override, checked
                    in middleware before anything else, exempt paths only
                    /health, /admin/halt, /admin/unlock
  telegram.py       Mid-tier/UI-tier approval channel (separate bot from
                    hermes's own messaging bot, by design)
  spread.py         Multi-model "spread" comparison endpoint (separate
                    budget/approval path, same building blocks)
  routes/
    chat.py         POST /v1/chat/completions — the main path
    spread.py       POST /v1/spread
    admin.py        /health, /admin/would-block/{agent}, /admin/halt,
                    /admin/unlock, /admin/surge*, /v1/run/{id}/outcome
bin/
  llm-gate          CLI: surge --minutes N --extra-cap X [--allow-frontier]
                    --reason "..." | --end | --status
  run-agent         Resource-gate wrapper (see below), reads agents.yaml
ports.yaml           The routing table — see next section
agents.yaml          Resource-gate weight classes for bin/run-agent
tests/               57 tests (pytest + respx), tests/conftest.py builds an
                    isolated temp state dir + fake ports.yaml/registry
scripts/
  verify_migration.sh  End-to-end live-traffic smoke test across all agents
```

Deployed as a systemd --user service (`llm-gate.service`), `uv run python
-m app.server`, binds `127.0.0.1:8787` (the actual FastAPI app listens on
one port; 8788-8792 work via the same process — see port-as-identity
below), `Restart=on-failure`.

## Port-as-identity (the actual routing mechanism)

`ports.yaml` is the current source of truth. Every real port is `enforce`
as of 2026-07-25:

| Port | agent_id | require_run_id | enforcement | Notes |
|---|---|---|---|---|
| 8787 | manual | true | enforce | ad-hoc/curl testing |
| 8788 | isaura | false | enforce | flipped 2026-07-24 |
| 8789 | hermes | false | enforce | flipped 2026-07-25, see below |
| 8790 | — | — | — | reserved/unused, not "claw" |
| 8791 | dcode | false | enforce | flipped 2026-07-24 |
| 8792 | opencode | false | enforce | entry unused — opencode doesn't route through the gate |

No agent needs to set a custom header to be identified — pointing its base
URL at a different port *is* the identity. This was the deliberate choice
(**Path B**) over header-injection (**Path A**) because most of these
clients' HTTP layers aren't reachable to inject headers into. hermes was
originally slated for Path A (it's custom Python) but turned out to be a
100+ module codebase with no clean `default_headers`/injection point found
— so it uses Path B like everyone else, with a synthesized run_id
(`{agent_id}_{sha8(first_system_prompt)}_{hour_bucket}`) when
`X-Run-Id` is absent. Coarse (same system prompt within the same hour
collapses to one run_id) but keeps per-run caps and loop detection working
instead of going fully meterless.

**enforcement semantics** (per-port, not global):
- `enforce` — deny-list and ui-only-ceiling checks always hard-block
  (categorical, no exceptions, this never changes). Budget caps, loop
  detection, dedup, and mid-tier approval also hard-block.
- `log_only` — deny-list/ui-only ceiling still always block. Everything
  else (budgets/loop/dedup/mid-approval) is recorded to
  `would_block_events` and let through — an observation window for a
  newly-repointed agent so a false-positive threshold doesn't break it on
  day one. Real spend is metered normally either way.
- `~/.llm-gate/HALT` overrides everything, in both modes, always.

Every real port is `enforce` today. isaura and dcode were flipped
2026-07-24 after review (thin real traffic — a couple of calls each — so
"reviewed and clean" reflects low volume more than heavy load-testing).
hermes was the last holdout, fixed and flipped 2026-07-25 (see "The hermes
bug" below).

## Routing and tiers

- **Upstream selection is fixed, not price-dependent**: model id prefixed
  `deepseek/` → DeepSeek direct API. Everything else → OpenRouter.
- **Tier** (`models_registry.py`): `blend = 0.75×input + 0.25×output`
  USD/M. `free` (blend ≤ 0), `cheap` (≤ $1.50), `mid` ($1.50–3.00),
  `ui_only` (> $3.00).
- DeepSeek's real callable models here are only `deepseek-v4-flash` and
  `deepseek-v4-pro` (confirmed live against api.deepseek.com — the
  "textbook" names `deepseek-chat`/`deepseek-reasoner` don't exist in this
  environment and 400). Both are cheap tier; DeepSeek has no real mid-tier
  model here. Bare names get aliased to the `deepseek/`-prefixed real
  equivalents so callers using provider-catalog-style names (openclaw,
  hermes) don't 400.
- **Deny list** (`deny_list.py`, never rewritten, always 403, never
  relaxed even in `log_only`): `openrouter/auto` (root cause of a past $25
  burn), `perplexity/sonar*`, `*:online`, `*:extended`.
- **Free tier**: `:free` OpenRouter model siblings, only discovered as
  siblings of models already trusted cheap. Gated by task class
  (`X-Task-Class` header, or a port's `default_task_class` — only
  opencode's port has one set, `"experiment"`). `isaura` is hardcoded
  (in `chat.py`, not a config toggle) to never qualify, no exceptions.
  `:free` failures retry once on the paid sibling.
- **Fallback rule**: DeepSeek 5xx/timeout/empty-body → exactly one retry
  on OpenRouter, forced to a fixed cheap model
  (`google/gemini-2.5-flash`), logged as `fallback_hop`.
- **Surge mode**: `bin/llm-gate surge --minutes N --extra-cap X
  [--allow-frontier] --reason "..."` — a deliberate, logged, bounded cap
  escalation (max 1 active, max 3/7 days). Deny-list, free-tier gating,
  per-run-cap, and loop-detection scope never change during surge — only
  the budget ceiling does.

## Budgets and safety rails (`app/ledger.py`, `app/config.py`)

- Global daily soft $1.50 / hard $2.00
- Mid-tier daily aggregate cap $0.60
- Per-run-id: 100 calls / $0.25 max
- Per-agent hourly: $0.40
- Loop detection: same prompt hash 5× within 10 minutes → block
- Exact-match response cache, 24h TTL
- Mid/ui_only tiers require Telegram approval (separate bot from hermes's
  own messaging bot — deliberately not reused, so an approval-channel
  compromise can't also compromise the agent's own comms channel)

## The streaming problem (why so much of upstreams.py exists)

LangChain-based agent frameworks (dcode's middleware, hermes's own agent
loop) send `stream: true` internally regardless of what the end user sees.
DeepSeek/OpenRouter correctly respond with real SSE chunked streams, which
broke the gate's core assumption that every upstream response is one JSON
body it can parse for tokens/cost. Fix: `_forced_params()` in
`upstreams.py` hard-forces `stream: false` on every outgoing call,
regardless of caller; `sse.py` synthesizes a minimal SSE stream back to
any client that asked for one, built from the one real JSON response.

**The hermes bug (fixed 2026-07-25)**: hermes's real wire request also
always sets `stream_options: {"include_usage": true}` — built in
`chat_completion_helpers.py`, *after* hermes's own debug-dump is written,
which is why replaying a captured dump never reproduced the failure.
Paired with the gate's forced `stream: false`, that's an invalid
combination on OpenAI-wire APIs (`stream_options` only valid when
`stream: true`) and DeepSeek 400'd on it. `_forced_params()` now also pops
`stream_options`. `chat.py`'s error handler was also fixed to surface the
real upstream response body (truncated to 300 chars) instead of
`str(exc)`, which is what made this take an Explore-agent source-code
trace instead of a one-line log read — worth remembering next time
something upstream rejects a request for a reason that isn't obvious.

## bin/run-agent — the resource gate

Separate from llm-gate (spend safety) — this is a **resource** safety
mechanism. Host is an i7-2600, 4c/8t: one heavy agent at a time. Reads
`agents.yaml` for weight classes (hermes=light/always-on,
claw=medium/openclaw gateways, dcode=heavy, opencode=heavy) and
`pgrep_pattern` to detect what's already running, refusing to launch a
second heavy agent if thresholds (`loadavg_1m_max: 6.0`,
`mem_available_min_mb: 2500`) are breached. **Design principle: it never
auto-kills another agent** — refuse-and-instruct is the only failure mode.
hermes and claw run as long-lived systemd services outside this wrapper's
control; it only reports on/resource-gates them, never (re)starts them.

## Why opencode is outside the gate

Architecture decision, not an oversight: opencode is a self-contained
backstop with its own OpenRouter key (`$1/day` limit, confirmed via
OpenRouter's key-info endpoint — `limit=1, limit_reset=daily`), used only
for free-tier models. It auto-detects `OPENROUTER_API_KEY` as an env var
natively (`opencode auth list` shows it under "Environment") — no gate
mediation, the daily key cap is the backstop instead of gate-side budget
tracking. Its `ports.yaml` entry (8792) is kept reserved but genuinely
unused.

## Real infra map

| What | Where |
|---|---|
| llm-gate source | `/home/a/llm-gate` |
| llm-gate state (ledger, cache, HALT) | `~/.llm-gate/` |
| llm-gate service | `llm-gate.service` (systemd --user), `127.0.0.1:8787-8792` |
| hermes source | `~/.hermes/hermes-agent` (~100+ Python modules) |
| hermes services | `hermes-gateway.service`, `hermes-gateway-baphomet.service` (two separate profiles — `~/.hermes/.env` + `~/.hermes/profiles/baphomet/.env`), plus `hermes-cron-catchup.service` |
| isaura config | `~/.openclaw/openclaw.json` |
| openclaw services | `openclaw-gateway.service`, `openclaw-gateway-hermes.service` (hermes-profile openclaw instance — port 18790, **separate** from hermes-agent, untouched by this migration), `openclaw-hermes-gateway.service` |
| dcode source | `~/.deepagents` (`deepagents-code`, LangChain-based) |
| opencode | `~/.opencode` (separate Go CLI binary, own PATH entry in `~/.bashrc`) |
| Provider keys | `~/llm-gate/.env` ONLY — `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_APPROVAL_BOT_TOKEN`, `TELEGRAM_APPROVAL_CHAT_ID`. Agents never hold real keys once repointed — dcode/hermes point at `127.0.0.1:87xx/v1` via env vars (`DEEPSEEK_API_BASE`, `DEEPSEEK_BASE_URL`) instead. |
| buying-agent repo | `/home/a/buying-agent` — **public** on GitHub (`andreengineer/buying-agent`), by explicit decision; `neuropsych/` was removed from it, not the repo made private |

## Standing constraints (do not relax without the user re-confirming)

- `openrouter/auto` and other deny-listed patterns are **never** rewritten
  to a cheap model — always 403, so the caller gets fixed at the source.
- Free-tier/deny-list/ui-only-ceiling checks are categorical, never
  relaxed, even in `log_only` mode.
- `~/.llm-gate/HALT` is a full manual override regardless of enforcement
  mode or an active surge.
- Per-run-cap and loop-detection scopes never change during surge.
- `bin/run-agent` never auto-kills another agent.
- Real API keys live only in `~/llm-gate/.env`.
- `buying-agent` repo stays public (explicit decision, don't revisit
  without being asked).
- Git: no force-push, no skipped hooks, no push without explicit
  confirmation each time.

## What's actually open right now

Nothing blocking. As of 2026-07-25:
- All 5 real gate ports are `enforce`.
- 57/57 tests passing.
- Known low-priority deferred item (`TODO.md`): `spread.py:173` should
  surface the real upstream error body the same way `chat.py` now does —
  not done, not urgent (spread errors are per-slot, not the primary
  response path).
- isaura/dcode's `enforce` review was done on thin real traffic (a couple
  of calls each) — worth a second look once more real volume accumulates,
  not currently a blocker.
- OmniRoute-as-free-tier-upstream is explicitly deferred (see
  `MIGRATION.md`'s "Deferred" section) — OmniRoute itself was decommissioned
  as a redundant product earlier in this build; only the "use it as a
  free-tier upstream" idea is parked, not required.
- Installing the `claude-code-harness` Claude Code plugin is the user's
  own action item, not a code change.

If picking a next step: there isn't a queued task. Good candidates if the
user wants forward motion: (1) revisit isaura/dcode's would-block data
once more real traffic has landed, (2) the spread.py TODO, (3) whatever
the user actually asks for next — this doc exists so that question can be
answered with real state instead of re-discovery.
