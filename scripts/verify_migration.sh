#!/usr/bin/env bash
# Run to check the invariants the migration plan cares about: no agent
# holds a raw provider key once it's behind llm-gate, every repointed
# agent's base_url actually points at the gate (not just "doesn't say
# api.deepseek.com/openrouter.ai" — a missing override falls back to the
# real default just as badly), the ledger is receiving real traffic per
# agent, and HALT is a true global kill switch.
set -uo pipefail

# --- HALT safety (incident 2026-09-28, root cause) --------------------------
# The HALT test below must never leave prod halted. The old trap only fired on
# normal exit and left ~/.llm-gate/HALT behind on SIGKILL or an rm failure —
# exactly how the kill switch leaked for 7 days. Two guards now:
#   1. halt_acquire/halt_release preserve any *pre-existing* HALT: the script
#      only removes the HALT it created, so running it never un-halts prod.
#   2. cleanup is bound to EXIT/INT/TERM/HUP.
# --no-halt skips the destructive block entirely (prod-safe / CI).
HALT_FILE="${LLM_GATE_HALT:-$HOME/.llm-gate/HALT}"
HALT_PREEXISTING=0
NO_HALT=0
SELF_TEST=0

halt_acquire() {
    if [ -e "$HALT_FILE" ]; then
        HALT_PREEXISTING=1
    else
        HALT_PREEXISTING=0
        mkdir -p "$(dirname "$HALT_FILE")"
        : > "$HALT_FILE"
    fi
}

halt_release() {
    # Restore prior state: only remove HALT if *we* created it.
    [ "$HALT_PREEXISTING" -eq 0 ] && rm -f "$HALT_FILE"
    return 0
}

trap 'halt_release' EXIT INT TERM HUP

self_test() {
    local tmp rc=0
    tmp="$(mktemp -d)"
    HALT_FILE="$tmp/HALT"

    # 1. no pre-existing HALT -> acquire creates, release removes
    halt_acquire
    [ -e "$HALT_FILE" ] || { echo "FAIL: acquire did not create HALT"; rc=1; }
    halt_release
    [ -e "$HALT_FILE" ] && { echo "FAIL: release left HALT behind"; rc=1; }

    # 2. pre-existing HALT -> acquire and release both preserve it
    : > "$HALT_FILE"
    halt_acquire
    halt_release
    [ -e "$HALT_FILE" ] || { echo "FAIL: release removed a pre-existing HALT (would un-halt prod)"; rc=1; }

    rm -rf "$tmp"
    [ "$rc" -eq 0 ] && echo "OK: HALT acquire/release semantics"
    return "$rc"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-halt)   NO_HALT=1 ;;
        --self-test) SELF_TEST=1 ;;
        -h|--help)
            echo "usage: $0 [--no-halt] [--self-test]"
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$SELF_TEST" -eq 1 ]; then
    self_test
    exit $?
fi

fail=0
GATE_HOST="127.0.0.1"
GATE_PORTS=(8787 8788 8789 8791 8792)  # 8790 (claw) intentionally absent — no real system maps there

echo "== ports.yaml: no dead 'claw' port entry =="
# Match an actual mapping line (agent_id: "claw"), not prose in comments
# that legitimately explain why claw was removed.
if grep -E '^\s*[0-9]+:.*agent_id:\s*"claw"' ~/llm-gate/ports.yaml >/dev/null 2>&1; then
    echo "FAIL: ports.yaml still has a claw port mapping — confirmed dead, should be removed"
    fail=1
else
    echo "OK"
fi

echo
echo "== opencode: should have zero paid-provider keys or URLs in its config =="
if grep -riE "sk-[a-zA-Z0-9]{10,}|api\.deepseek\.com|openrouter\.ai" ~/.config/opencode/opencode.jsonc 2>/dev/null; then
    echo "FAIL: opencode.jsonc contains a raw key or provider URL"
    fail=1
else
    echo "OK"
fi

echo
echo "== repointed agents: base_url must point AT the gate, not just avoid the real one =="
declare -A EXPECTED_BASE_URL=(
    ["$HOME/.hermes/.env"]="127.0.0.1:8789"
    ["$HOME/.hermes/profiles/baphomet/.env"]="127.0.0.1:8789"
    ["$HOME/.hermes/config.yaml"]="127.0.0.1:8789"
    ["$HOME/.hermes/profiles/baphomet/config.yaml"]="127.0.0.1:8789"
    ["$HOME/.openclaw/openclaw.json"]="127.0.0.1:8788"
    ["$HOME/.bashrc"]="127.0.0.1:8791"  # dcode's DEEPAGENTS_CODE_DEEPSEEK_API_BASE
)
for f in "${!EXPECTED_BASE_URL[@]}"; do
    expected="${EXPECTED_BASE_URL[$f]}"
    [ -f "$f" ] || { echo "SKIP ($f): file missing"; continue; }
    # Strip comments first (# for shell/yaml/toml-style, // for jsonc) so a
    # help-text line like "# Get your key at: https://openrouter.ai/keys"
    # doesn't false-positive against an actual base_url config value.
    active_lines=$(grep -vE '^\s*(#|//)' "$f" 2>/dev/null)
    if echo "$active_lines" | grep -q "api\.deepseek\.com\|openrouter\.ai"; then
        echo "FAIL ($f): still references a real provider URL directly (outside comments)"
        fail=1
    elif ! grep -q "$expected" "$f" 2>/dev/null; then
        echo "FAIL ($f): does not reference expected gate address $expected — repoint may be missing entirely"
        fail=1
    else
        echo "OK ($f) -> $expected"
    fi
done

echo
echo "== llm-gate itself: is it the only place holding real provider keys =="
if [ -f ~/llm-gate/.env ]; then
    echo "OK: ~/llm-gate/.env exists (expected location for both keys)"
else
    echo "FAIL: ~/llm-gate/.env missing"
    fail=1
fi

echo
echo "== ledger: real traffic landing per repointed agent (last 24h) =="
if command -v sqlite3 >/dev/null 2>&1 && [ -f ~/.llm-gate/spend.db ]; then
    sqlite3 ~/.llm-gate/spend.db \
        "select agent, count(*) from calls where ts >= strftime('%s','now')-86400 group by agent;" \
        | while IFS='|' read -r agent count; do
        echo "  $agent: $count calls in last 24h"
    done
    for agent in isaura hermes dcode; do
        count=$(sqlite3 ~/.llm-gate/spend.db \
            "select count(*) from calls where agent='$agent' and ts >= strftime('%s','now')-86400;")
        if [ "${count:-0}" -eq 0 ]; then
            echo "WARN ($agent): zero calls in the ledger in the last 24h — repointed but not actually exercised?"
        fi
    done
else
    echo "SKIP: sqlite3 or ledger db not found"
fi

echo
echo "== HALT is a true global kill switch across every live port =="
echo "   (/health is deliberately HALT-exempt so monitoring can tell 'halted'"
echo "   from 'down' — the actual gated path is /v1/chat/completions)"
if [ "$NO_HALT" -eq 1 ]; then
    echo "SKIP: --no-halt — destructive kill-switch test not exercised"
    halt_ok=0
else
halt_acquire
sleep 1
halt_ok=1
health_status=$(curl -sS --max-time 5 "http://${GATE_HOST}:8787/health" 2>/dev/null)
if ! echo "$health_status" | grep -q '"status":"halted"' && ! echo "$health_status" | grep -q '"status": "halted"'; then
    echo "FAIL: /health does not report status=halted while HALTed"
    fail=1
fi
for port in "${GATE_PORTS[@]}"; do
    status=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 -X POST \
        "http://${GATE_HOST}:${port}/v1/chat/completions" \
        -H "Content-Type: application/json" -H "X-Run-Id: verify_halt" \
        -d '{"model":"deepseek/deepseek-v4-flash","messages":[]}' 2>/dev/null)
    if [ "$status" != "503" ]; then
        echo "FAIL (port $port): expected 503 on /v1/chat/completions while HALTed, got $status"
        halt_ok=0
        fail=1
    fi
done
[ "$halt_ok" -eq 1 ] && echo "OK: all ${#GATE_PORTS[@]} ports returned 503 on the gated path while HALTed"
halt_release
sleep 1
status=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "http://${GATE_HOST}:8787/health" 2>/dev/null)
if [ "$status" = "200" ]; then
    echo "OK: unlocked, gate healthy again"
else
    echo "FAIL: gate did not return to healthy (200) after removing HALT, got $status"
    fail=1
fi
fi  # end HALT test (NO_HALT gate)

echo
echo "== resource gate: run-agent refuses a heavy agent while claw/openclaw is alive =="
if pgrep -f "openclaw/dist/index.js" >/dev/null 2>&1; then
    # capture first, don't pipe directly to grep — with pipefail set, the
    # pipeline reports run-agent's own nonzero (refusal) exit regardless of
    # whether grep matched
    run_agent_out=$(~/llm-gate/bin/run-agent dcode 2>&1 || true)
    if echo "$run_agent_out" | grep -q "REFUSED"; then
        echo "OK: run-agent correctly refused"
    else
        echo "FAIL: run-agent did not refuse dcode while openclaw is running"
        fail=1
    fi
else
    echo "SKIP: openclaw not currently running, can't exercise this check"
fi

echo
echo "== no client-side fallback chains in any agent config (ESCALATION_POLICY §4) =="
# A fallback list inside an agent config is a routing decision the gate cannot
# see: the agent resolves e.g. openrouter/auto to a concrete model name BEFORE
# the request arrives, so the gate's deny list never fires. The gate owns all
# fallback. Checks EVERY profile, not only the known ones.
fb_hits=0
while IFS= read -r cfg; do
    case "$cfg" in *.bak*|*last-good*|*pre-update*|*pre-cost*|*.migrated) continue ;; esac
    hit=$(python3 -c "
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit()
if not isinstance(d,dict): sys.exit()
ag=d.get('agents')
if not isinstance(ag,dict): sys.exit()
mdl=ag.get('defaults',{}).get('model',{})
if isinstance(mdl,dict) and mdl.get('fallbacks'):
    print('%s: %d fallback(s): %s' % (sys.argv[1], len(mdl['fallbacks']), mdl['fallbacks']))
for a in ag.get('list',[]) or []:
    m=(a or {}).get('model',{})
    if isinstance(m,dict) and m.get('fallbacks'):
        print('%s [agent %s]: %d fallback(s)' % (sys.argv[1], a.get('id'), len(m['fallbacks'])))
" "$cfg" 2>/dev/null)
    if [ -n "$hit" ]; then
        echo "FAIL: $hit"
        fb_hits=$((fb_hits + 1))
    fi
done < <(find "$HOME" -maxdepth 3 -name "openclaw*.json" -not -path "*/node_modules/*" 2>/dev/null)

if [ "$fb_hits" -eq 0 ]; then
    echo "OK: no agent config declares a fallback chain"
else
    echo "FAIL: $fb_hits agent config(s) carry client-side fallback chains — strip them, the gate owns fallback"
    fail=1
fi

exit $fail
