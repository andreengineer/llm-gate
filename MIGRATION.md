# llm-gate migration status

Authoritative plan is `GATE_MIGRATION_PLAN.md` (in Downloads, successor to
`HERMES_SPEND_GUARDRAILS.md`), with cadence compressed per `IMPROVEMENTS.md`
(isaura 4h log-only instead of 24h; hermes/claw/dcode same-day 2h each
instead of one-per-day). This file just tracks what's actually done.

## Done

- **Step 0 — key rotation.** New DeepSeek + OpenRouter keys live in
  `~/llm-gate/.env` only. Old keys confirmed revoked (tested all 7 historical
  key values found on disk against each provider's auth-check endpoint —
  every one, including today's old ones, returns 401). Public buying-agent
  repo confirmed clean of any key pattern.
- **Step 1 — approval bot.** Separate Telegram bot (not the Hermes gateway
  bot) wired in as `TELEGRAM_APPROVAL_BOT_TOKEN`/`TELEGRAM_APPROVAL_CHAT_ID`.
  Verified end-to-end against production: message delivered, 90s timeout ->
  `429 approval_timeout`, zero upstream spend.
- **Step 2 — port-as-identity.** No agent needs header-injection code to be
  identified anymore. `~/llm-gate/ports.yaml` maps 6 ports to fixed
  identities; `app/server.py` binds all of them in one process (shared
  ledger/registry/telegram state, single price-refresh loop):

  | Port | agent_id | X-Run-Id required? |
  |---|---|---|
  | 8787 | manual | yes (ad-hoc/curl testing) |
  | 8788 | isaura | no — synthesized if absent |
  | 8789 | hermes | no — synthesized if absent (see below, not Path A) |
  | 8790 | claw | dead — no real system maps here, see below |
  | 8791 | dcode | no — synthesized if absent |
  | 8792 | opencode | no — synthesized if absent |

  "Claw" was later confirmed to be a role label for the same isaura/8788
  gateway, not a separate system — its old Playwright duties live inside
  isaura's own code or moved to Hermes's Browserbase integration. Port 8790
  has no real target; scheduled for removal.

  Synthesis: `{agent_id}_{sha8(first system prompt)}_{hour_bucket}` — coarse
  but keeps per-run cap and loop detection alive instead of going meterless.
  X-Agent-Id header is gone entirely (superseded by port identity). Verified
  live: real no-header call on 8788 returned a real DeepSeek response with
  `X-Gate-Agent-Id: isaura` / `X-Gate-Run-Id: isaura_<hash>_<hour>`. 30 tests
  passing.
- **OmniRoute decommissioned**: service stopped+disabled, removed from
  opencode config, dead credential scrubbed.
- **Resource gate**: `~/llm-gate/agents.yaml` + `bin/run-agent`, verified
  live (correctly refuses `dcode` while `claw`/openclaw is running).
- **Public repo exposure fixed**: `neuropsych/` (personal health data) was
  live on the actually-public `andreengineer/buying-agent` repo despite
  CLOUD_POLICY.md claiming it's private. Removed from the tree and pushed
  (repo stays public per decision; history still has it — a full scrub
  would need `git filter-repo` + force-push, not done).
- **Step 3 — isaura repointed and live.** `~/.openclaw/openclaw.json`'s
  `models.providers.deepseek.baseUrl` -> `http://127.0.0.1:8788` (the
  `openclaw-gateway.service` on port 18789, i.e. the default/isaura agent —
  NOT `openclaw-hermes-gateway.service` on 18790, which has its own separate
  config file `openclaw-hermes.json` and is untouched). Set to `log_only` in
  `ports.yaml` for a 4h window (2026-07-24 02:10 -> ~06:10). Real per-port
  `enforcement` mode is now an actual behavior, not a status flag: deny-list
  and the ui-only ceiling always hard-block (categorical, costs nothing to
  block); budget caps/loop-detection/dedup/mid-tier-approval observe-and-log
  to a new `would_block_events` table and let the call through when a port
  is `log_only`. Query via `GET /admin/would-block/isaura?since_hours=4`.
  Also fixed along the way: openclaw sends bare DeepSeek model ids
  (`deepseek-v4-flash`, not `deepseek/deepseek-v4-flash`) and its real API
  name for the reasoning model is `deepseek-reasoner` (the gate had
  invented `deepseek-r1`) — both now resolve via an alias table before any
  allowlist/pricing lookup. Verified end-to-end against the live service.
  36 tests passing.

- **Step 4 — hermes repointed and enforce (flipped 2026-07-25).** hermes-agent
  (`~/.hermes/hermes-agent`, ~100+ Python modules) has no clean config-only
  way to inject `X-Run-Id` (checked `providers.py`'s override path and
  `ProviderProfile.default_headers` — real but plugin-file-only, not a
  config toggle) — so hermes uses Path B like everyone else, not Path A as
  originally planned. `require_run_id: false` in `ports.yaml`.
  Repointed: `~/.hermes/.env` + `~/.hermes/profiles/baphomet/.env` get
  `OPENROUTER_BASE_URL=http://127.0.0.1:8789/v1` AND
  `DEEPSEEK_BASE_URL=http://127.0.0.1:8789/v1` (hermes has two *separate*
  provider paths — OpenRouter fallback and direct DeepSeek primary — both
  needed patching). Also: `~/.hermes/config.yaml` and
  `~/.hermes/profiles/baphomet/config.yaml` both had an explicit
  `providers.deepseek.base_url: https://api.deepseek.com/v1` that
  overrides the env var — patched both.
  Two real bugs found and fixed via live hermes traffic (not caught by any
  test until real traffic hit them):
  1. hermes's own config references `deepseek-chat`/`deepseek-reasoner`
     (DeepSeek's real-world API names) — confirmed via live replay against
     api.deepseek.com that neither exists in this environment; only
     `deepseek-v4-flash`/`deepseek-v4-pro` are real. Removed the fabricated
     registry entries, aliased the real-world names to their closest real
     equivalent instead of a fake entry that always 400s.
  2. `upstreams.py` crashed with an unhandled 500 when an upstream returned
     a 2xx with an empty body (`resp.json()` on empty content). Now caught
     and treated as a transient failure (same one-shot fallback as a 5xx).
  3. **Root cause of the empty-body pattern, found via dcode traffic and
     confirmed to explain hermes's failures too**: LangChain-based agent
     frameworks (dcode's agent middleware, and hermes's own agent loop)
     send `stream: true` internally even when the end-user experience isn't
     visibly streaming. DeepSeek/OpenRouter correctly respond with real SSE
     chunked streams; the gate was calling `resp.json()` on raw
     `data: {...}\n\n` bytes. Fixed: `_forced_params` now hard-forces
     `stream: false` on every upstream call regardless of the caller, and
     synthesizes a minimal SSE stream back to the client if it asked for
     one (`app/sse.py`). Also fixed along the way: `ledger.prompt_hash`
     and `cache.trim_old_tool_results` crashed on list-style message
     content (`[{"type":"text","text":...}]`, not a plain string) — a
     real pattern from dcode's LangChain client.
  **dcode is fully verified working end-to-end** after this fix (real
  call succeeded, single clean DeepSeek call, correctly billed in the
  ledger — see Step 4 dcode entry below).
  **Hermes's residual 400 — root-caused and fixed.** The streaming fix
  above only forced `stream: false`; it never stripped `stream_options`.
  hermes's real wire request (built in `hermes_cli/agent/
  chat_completion_helpers.py`, *after* its own debug dump is written —
  which is why replaying the dump never reproduced it) always sends
  `stream_options: {"include_usage": true}` alongside `stream: true`.
  Paired with the gate's forced `stream: false`, that's an invalid
  combination on OpenAI-wire APIs (`stream_options` only valid when
  `stream: true`), and DeepSeek 400'd on it. Fixed: `_forced_params` now
  also pops `stream_options`. Also fixed alongside it: `chat.py`'s error
  handler now includes the real upstream response body (truncated) in
  raised `HTTPException`s instead of discarding it — this is what made
  the root cause take an Explore pass through hermes's own source instead
  of a one-line log read. Verified live: real `hermes -z` call succeeded
  cleanly post-fix, single clean DeepSeek call, correctly billed. No
  spend-safety issue either way throughout (failed calls never get
  billed).
- **Step 4 — dcode repointed and verified.** `deepagents-code` is
  LangChain-based; its `deepseek` provider reads `DEEPSEEK_API_BASE`
  (canonical env var name, confirmed via `langchain_deepseek`'s source —
  default is `https://api.deepseek.com/v1`, so needs the `/v1` suffix like
  hermes). But dcode's stored credential (`dcode auth`) has no base_url
  ever set via its `/auth` flow, and `apply_stored_credentials()` **clears
  the plain env var on every run** when that's the case (documented
  behavior: prevents an inherited gateway URL from leaking when the field
  is blank). The `DEEPAGENTS_CODE_`-prefixed override
  (`DEEPAGENTS_CODE_DEEPSEEK_API_BASE`) survives that clearing via
  `resolve_env_var`'s dynamic-prefix mechanism — set in `~/.bashrc`. Set
  to `log_only` in `ports.yaml`. Verified end-to-end: real `dcode -n`
  call succeeded, ledger row landed for `agent='dcode'`.

- **`scripts/verify_migration.sh` expanded and green end-to-end** against
  the live system: per-agent base_url must point AT the gate (not just
  avoid the real provider URL), per-agent ledger traffic, HALT-is-global,
  run-agent resource gate. Found and fixed 3 real bugs in the script
  itself while running it for real: false-positive "claw" match against
  its own explanatory comments, false-positive provider-URL match against
  a "get your key at:" help comment in hermes's `.env`, and a classic
  `pipefail` gotcha (`refusing_cmd | grep -q pattern` reports failure
  under `set -o pipefail` even when grep matches, because the upstream
  command's own nonzero exit wins).
- **Free tier** (SONNET.md §1): new tier below cheap for genuine $0
  OpenRouter `:free` model variants, discovered at boot only as siblings
  of models already trusted as cheap (never a blanket import). Gated by
  `X-Task-Class` (header or port `default_task_class` — only opencode/8792
  has one); isaura is hardcoded to never qualify, no exceptions. 429/5xx
  on a `:free` call retries once on the paid cheap sibling
  (`free_fallback`, alerts >20/day). Free calls cost $0 but still count
  toward per-run caps and loop detection.
- **Surge mode** (SONNET.md §2): `bin/llm-gate surge --minutes N
  --extra-cap X [--allow-frontier] --reason "..."` — the only sanctioned
  way caps move, always explicit/time-boxed/reasoned. Verified end-to-end
  against the live service including the real Telegram start/end
  announcement. Max 1 active, max 3 per rolling 7 days.
- **Step 5 — opencode wired, does not go through the gate (by design).**
  New OpenRouter key ($1/day limit, not the originally-planned $0 — Andre's
  call after seeing the real limit via OpenRouter's key-info endpoint;
  $1/day is still a tiny bounded backstop and opencode only selects
  free-tier models anyway). opencode auto-detects `OPENROUTER_API_KEY` as
  an environment variable natively (confirmed via `opencode auth list`,
  which shows it under "Environment" once set) — no interactive
  `auth login` TUI needed, and nothing sensitive lands in
  `~/.config/opencode/` or `~/.local/share/opencode/auth.json` (both stay
  clean, verified by the existing raw-key grep). Set in `~/.bashrc`.
  Verified end-to-end: real call to `openrouter/google/gemma-4-26b-a4b-it:free`
  succeeded.

## Not done yet

All 5 real gate ports (manual, isaura, hermes, dcode, opencode) are now
`enforcement: "enforce"`. isaura and dcode were flipped 2026-07-24 21:39;
hermes was flipped 2026-07-25 00:59 after the `stream_options` fix, a
clean live re-test, and an empty `would-block` summary over 48h. isaura
and dcode were reviewed under thin real traffic (a couple of calls each),
so the clean would-block data reflects "not enough traffic to observe"
more than "reviewed under load and found clean" — worth another look once
more real traffic accumulates, but not a blocker.

## Deferred — OmniRoute as free-tier upstream

Not happening now. Recorded so a future session doesn't have to
re-derive the reasoning or the guardrails around it.

- **Trigger to revisit**: only after 14 days of real free-tier traffic, if
  the ledger shows `free_fallback` consistently >20/day — i.e. OpenRouter's
  `:free` quota is the actual binding constraint, not a one-off blip.
  Check: `sqlite3 ~/.llm-gate/spend.db "select date(ts,'unixepoch'),
  count(*) from calls where free_fallback=1 group by 1;"`.
- **If triggered, the only acceptable shape**: OmniRoute running headless
  (docker base profile) as an ADDITIONAL upstream of llm-gate, for the
  free tier only, holding exclusively free-provider keys.
- **Hard prohibitions, recorded now before any future revisit**: no
  Anthropic/OpenAI/subscription accounts ever connected to it; ToS-flagged
  providers disabled; no TLS-stealth or MITM features enabled; the gate
  keeps all enforcement (OmniRoute would never bypass it — it's just
  another upstream behind the same policy layer). Rationale:
  subscription-harvesting risk could get the Claude account that runs
  Claude Code itself banned.
- **Do not install or configure anything under this item now** — this is
  a text-only marker, not a task.

## Operating llm-gate

- Health (any port, all identical): `curl 127.0.0.1:8787/health`
- Halt everything (all 6 ports): `touch ~/.llm-gate/HALT` or
  `curl -X POST 127.0.0.1:8787/admin/halt`
- Unlock: `curl -X POST 127.0.0.1:8787/admin/unlock`
- Logs: `journalctl --user -u llm-gate -f`
- Restart after `.env`/`ports.yaml` changes: `systemctl --user restart llm-gate`
- Spend ledger: `sqlite3 ~/.llm-gate/spend.db "select * from calls order by ts desc limit 20;"`
