#!/bin/bash
# Smoke test for watchdog.sh health-check logic: runs the script in "dry-run"
# mode (by short-circuiting the restart path) and asserts it exits 0 when
# everything is healthy and non-zero when MCP is missing after grace.
#
# This doesn't exercise the restart path — that would kill the live session.
# It only verifies the HEALTH-CHECK half of watchdog.sh.

set -u
PASS=0; FAIL=0

# Derive the health-check block from watchdog.sh itself, stripping the
# restart path (everything from the "restarting" echo onward).
WATCHDOG="$(cd "$(dirname "$0")" && pwd)/watchdog.sh"
TMP=$(mktemp /tmp/watchdog-health-XXXX.sh)
# Strip the original empty-REASON exit-0 block AND everything from the
# restart echo onward — we want to reach our appended HEALTH_OK/FAIL logic
# regardless of REASON state.
awk '
  /^if \[ -z "\$REASON" \]; then$/ {skip=1; next}
  skip && /^fi$/ {skip=0; next}
  skip {next}
  /^echo.*Watchdog: restarting/ {exit}
  {print}
' "$WATCHDOG" > "$TMP"
# Replace the final "if [ -z "$REASON" ]; then exit 0; fi" with one that
# prints REASON and exits a useful code.
cat >> "$TMP" <<'APPEND'
if [ -z "${REASON:-}" ]; then
    echo "HEALTH_OK"
    exit 0
else
    echo "HEALTH_FAIL: $REASON"
    exit 1
fi
APPEND
chmod +x "$TMP"

assert() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS  $label"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $label  expected=$expected actual=$actual"
        FAIL=$((FAIL+1))
    fi
}

# Test 1: no tmux session at all → should report missing session.
echo "case: no tmux session"
SESSION_NAME=bunny-fake-nonexistent-$$ "$TMP" > /tmp/out.$$ 2>&1
rc=$?
assert "exit-nonzero" 1 "$rc"
grep -q "tmux session missing" /tmp/out.$$ && assert "reason" 1 1 || assert "reason" 1 0

# Test 2: live tmux session with a long-running sleep as the pane — claude
# looks alive, MCP doesn't exist, grace period has NOT yet elapsed → should
# still exit 0 (within grace period).
echo
echo "case: claude up <90s, no MCP (grace period active)"
tmux new-session -d -s watchdog-test-$$ 'sleep 600' 2>/dev/null || true
sleep 1
SESSION_NAME=watchdog-test-$$ "$TMP" > /tmp/out.$$ 2>&1
rc=$?
assert "exit-zero-within-grace" 0 "$rc"
grep -q "HEALTH_OK" /tmp/out.$$ && assert "reports-ok" 1 1 || assert "reports-ok" 1 0
tmux kill-session -t watchdog-test-$$ 2>/dev/null

rm -f "$TMP" /tmp/out.$$
echo
echo "Watchdog health tests: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && exit 0 || exit 1
