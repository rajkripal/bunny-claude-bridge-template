#!/bin/bash
# Offline tests for the auth-recovery pair (auth-monitor.sh + login-relay.sh).
#
# Nothing here touches real auth, tmux sessions, launchd or Telegram. Each case
# copies the scripts into a throwaway repo dir (so no real .env is sourced),
# points HOME and BRIDGE_LOG_DIR at temp dirs, and shadows claude, curl, tmux,
# launchctl, pgrep, lsof and security with fakes that only record their calls.
#
# Covers:
#   - the /login trigger is an exact whole-message match, owner chat only
#   - login-relay exits without sending anything when auth is already healthy
#   - the full relay flow: URL extracted through OSC-8 escapes, code read from
#     the owner's chat only, fed through the FIFO, session dropped on success

set -u
PASS=0; FAIL=0
SRC="$(cd "$(dirname "$0")" && pwd)"
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

OWNER_CHAT=111
STRANGER_CHAT=222

check() {
    local label="$1"; shift
    if "$@"; then echo "  PASS  $label"; PASS=$((PASS+1))
    else echo "  FAIL  $label"; FAIL=$((FAIL+1)); fi
}

# Build a fresh sandbox: repo copy, fake bin dir, log dir, home dir.
setup() {
    BOX=$(mktemp -d "$ROOT/box.XXXX")
    mkdir -p "$BOX/repo/scripts" "$BOX/repo/cron" "$BOX/bin" "$BOX/logs" "$BOX/home" "$BOX/state"
    cp "$SRC/auth-monitor.sh" "$SRC/login-relay.sh" "$BOX/repo/scripts/"
    cat > "$BOX/repo/cron/telegram-notify.sh" <<EOF
#!/bin/bash
echo "notify \$*" >> "$BOX/calls.log"
EOF
    for tool in tmux launchctl pgrep lsof security; do
        cat > "$BOX/bin/$tool" <<EOF
#!/bin/bash
echo "$tool \$*" >> "$BOX/calls.log"
EOF
    done
    # Fake curl: records every call; getUpdates call 1 returns stale.json,
    # later calls return updates.json (falls back to an empty result).
    cat > "$BOX/bin/curl" <<EOF
#!/bin/bash
echo "curl \$*" >> "$BOX/calls.log"
case "\$*" in
  *getUpdates*)
    n=\$(( \$(cat "$BOX/state/gu" 2>/dev/null || echo 0) + 1 )); echo \$n > "$BOX/state/gu"
    if [ \$n -eq 1 ] && [ -f "$BOX/state/stale.json" ]; then cat "$BOX/state/stale.json"
    elif [ -f "$BOX/state/updates.json" ]; then cat "$BOX/state/updates.json"
    else echo '{"ok":true,"result":[]}'; fi ;;
  *) echo '{"ok":true}' ;;
esac
EOF
    # Fake claude: auth status reflects state/loggedin; auth login prints an
    # OSC-8 hyperlinked URL, reads one line of stdin, logs in if it's the code.
    cat > "$BOX/bin/claude" <<EOF
#!/bin/bash
case "\$1 \$2" in
  "auth status")
    if [ -f "$BOX/state/loggedin" ]; then echo '{"loggedIn": true}'; else echo '{"loggedIn": false}'; fi ;;
  "auth login")
    u='https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc123'
    printf 'Browser did not open? Use this URL:\n\033]8;;%s\033\\\\\033[4m%s\033[0m\033]8;;\033\\\\\n' "\$u" "\$u"
    printf 'Paste code here: '
    IFS= read -r line
    echo "\$line" > "$BOX/state/fed"
    [ "\$line" = "the-code-xyz" ] && touch "$BOX/state/loggedin"
    exit 0 ;;
esac
EOF
    chmod +x "$BOX/bin/"* "$BOX/repo/cron/telegram-notify.sh"
    : > "$BOX/calls.log"
}

run_env() {
    env -i PATH="$BOX/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin" HOME="$BOX/home" \
        BRIDGE_LOG_DIR="$BOX/logs" BRIDGE_SESSION_NAME="test-session-$$" \
        BRIDGE_LABEL_PREFIX="com.example.test" TELEGRAM_CHAT_ID="$OWNER_CHAT" \
        TELEGRAM_BOT_TOKEN="000:fake" CLAUDE_BIN="$BOX/bin/claude" CODE_WAIT_SECS=10 \
        "$@"
}

updates_json() {  # args: chat text [chat text ...]
    python3 - "$@" <<'PY'
import json, sys
a = sys.argv[1:]
res = [{"update_id": 100 + i, "message": {"chat": {"id": int(a[2*i])}, "text": a[2*i+1]}}
       for i in range(len(a) // 2)]
print(json.dumps({"ok": True, "result": res}))
PY
}

# ---------------------------------------------------------------------------
echo "auth-monitor: /login trigger parsing (dead auth, already alerted)"
trigger_case() {  # args: expected(yes|no) chat text
    local expected="$1" chat="$2" text="$3"
    setup
    echo "alerted=1" > "$BOX/logs/.auth-monitor-state"
    updates_json "$chat" "$text" > "$BOX/state/updates.json"
    run_env bash "$BOX/repo/scripts/auth-monitor.sh"
    local got=no
    grep -q "launchctl kickstart gui/[0-9]*/com.example.test.login-relay" "$BOX/calls.log" && got=yes
    check "$(printf '%-14q chat=%s -> %s' "$text" "$chat" "$expected")" [ "$got" = "$expected" ]
}
trigger_case yes "$OWNER_CHAT" "/login"
trigger_case yes "$OWNER_CHAT" "login"
trigger_case yes "$OWNER_CHAT" "  LOGIN "
trigger_case yes "$OWNER_CHAT" "/Login"
trigger_case no  "$OWNER_CHAT" "login later"
trigger_case no  "$OWNER_CHAT" "I can't login"
trigger_case no  "$OWNER_CHAT" "/login now"
trigger_case no  "$OWNER_CHAT" "logout"
trigger_case no  "$STRANGER_CHAT" "/login"

echo
echo "auth-monitor: healthy auth does nothing"
setup
touch "$BOX/state/loggedin"
run_env bash "$BOX/repo/scripts/auth-monitor.sh"; rc=$?
check "exit 0" [ "$rc" -eq 0 ]
check "no external calls" [ ! -s "$BOX/calls.log" ]

echo
echo "auth-monitor: strike 1 only logs, strike 2 stops bridge and alerts once"
setup
updates_json > "$BOX/state/updates.json"
run_env bash "$BOX/repo/scripts/auth-monitor.sh"
check "strike 1: no tmux/notify/curl" bash -c "! grep -qE '^(tmux|notify|curl)' '$BOX/calls.log'"
run_env bash "$BOX/repo/scripts/auth-monitor.sh"
check "strike 2: kills bridge session" grep -q "tmux kill-session -t test-session-$$" "$BOX/calls.log"
check "strike 2: alerts once" [ "$(grep -c '^notify' "$BOX/calls.log")" -eq 1 ]
check "alert tells owner to send /login" grep -q "send me:  /login" "$BOX/calls.log"
run_env bash "$BOX/repo/scripts/auth-monitor.sh"
check "strike 3: still only one alert" [ "$(grep -c '^notify' "$BOX/calls.log")" -eq 1 ]

# ---------------------------------------------------------------------------
echo
echo "login-relay: healthy-auth guard"
setup
touch "$BOX/state/loggedin"
run_env bash "$BOX/repo/scripts/login-relay.sh"; rc=$?
check "exit 0" [ "$rc" -eq 0 ]
check "curl never called" bash -c "! grep -q '^curl' '$BOX/calls.log'"
check "tmux never called" bash -c "! grep -q '^tmux' '$BOX/calls.log'"
check "logged the refusal" grep -q "auth already healthy" "$BOX/logs/login-relay.log"
check "pidfile cleaned up" [ ! -e "$BOX/logs/.login-relay.pid" ]

echo
echo "login-relay: full flow with fake claude + fake Telegram"
setup
updates_json "$OWNER_CHAT" "/login" > "$BOX/state/stale.json"
# Stranger's message, a re-sent trigger, then the real code (all in one batch).
updates_json "$STRANGER_CHAT" "attacker-code" "$OWNER_CHAT" "/login" "$OWNER_CHAT" "the-code-xyz" \
    > "$BOX/state/updates.json"
run_env bash "$BOX/repo/scripts/login-relay.sh"; rc=$?
check "exit 0" [ "$rc" -eq 0 ]
# The message body is multi-line, so the URL lands on a line of its own.
check "sent the clean OAuth URL" grep -qxF "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc123" "$BOX/calls.log"
check "URL has no escape bytes" bash -c "! grep -q \$'\\033' '$BOX/calls.log'"
check "fed exactly the owner's code" [ "$(cat "$BOX/state/fed" 2>/dev/null)" = "the-code-xyz" ]
check "dropped the stale session" grep -q "tmux kill-session -t test-session-$$" "$BOX/calls.log"
check "announced recovery" grep -q "Auth restored" "$BOX/calls.log"
check "sends only to owner chat" bash -c "! grep 'sendMessage' '$BOX/calls.log' | grep -qv 'chat_id=$OWNER_CHAT'"

echo
echo "Auth recovery tests: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
