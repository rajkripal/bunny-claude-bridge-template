#!/bin/bash
# login-relay.sh: remote Anthropic-auth recovery over Telegram, fully independent
# of Anthropic auth. Kicked by auth-monitor.sh (as its own launchd job,
# launchd/com.bridge.login-relay.plist) when the owner sends /login during a
# confirmed auth outage.
#
# Flow:
#   1. Run `claude auth login --claudeai` (stays on the Claude subscription, no
#      API key setup) with a controllable stdin (FIFO) and captured stdout.
#   2. Parse the OAuth URL from stdout and send it to the owner via bot token + curl.
#   3. Poll getUpdates (bot token, no Anthropic auth) for the pasted code.
#   4. Feed the code to the waiting login process, verify `claude auth status`.
#   5. On success, drop the stale session so the watchdog brings the bridge back
#      (its auth-gate re-opens once auth is healthy).
#
# Transport uses ONLY the bot token + curl + getUpdates, so it works when the
# assistant cannot reach Anthropic at all. The single getUpdates reader slot is
# free because auth-monitor.sh stops the bridge before kicking this, and the
# watchdog stands down while auth is dead (its auth-gate). One reader,
# repurposed, no second bot.
#
# Config comes from .env at the repo root (BRIDGE_LOG_DIR, BRIDGE_SESSION_NAME,
# ASSISTANT_NAME, TELEGRAM_CHAT_ID, optional CLAUDE_BIN). The bot token is read
# from ~/.claude/channels/telegram/.env unless TELEGRAM_BOT_TOKEN is already set.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

# Every setting below is env-overridable. Production gets them from .env; the
# tests set them directly so they can isolate logs, tokens, the session name and
# the claude binary and never touch the live bridge.
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/bridge-logs}"
LOG="$LOG_DIR/login-relay.log"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ASSISTANT="${ASSISTANT_NAME:-Assistant}"
OFFSET_FILE="$LOG_DIR/.relay-offset"
PID_FILE="$LOG_DIR/.login-relay.pid"
BRIDGE_SESSION="${BRIDGE_SESSION_NAME:-bridge}"
CODE_WAIT_SECS="${CODE_WAIT_SECS:-900}"   # 15 min to open the link and paste the code

# Resolve claude to an absolute path; launchd's PATH is minimal.
resolve_claude_bin(){
    if [ -n "${CLAUDE_BIN:-}" ]; then echo "$CLAUDE_BIN"; return; fi
    local c
    for c in "$(command -v claude 2>/dev/null)" "$HOME/.local/bin/claude" \
             /opt/homebrew/bin/claude /usr/local/bin/claude; do
        [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
    done
    echo "/opt/homebrew/bin/claude"
}
CLAUDE="$(resolve_claude_bin)"

mkdir -p "$LOG_DIR"
TOKEN="${TELEGRAM_BOT_TOKEN:-$(grep TELEGRAM_BOT_TOKEN "$HOME/.claude/channels/telegram/.env" 2>/dev/null | cut -d= -f2)}"

log(){ echo "[$(date)] $*" >> "$LOG"; }
send(){ curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" --data-urlencode "text=$1" >/dev/null 2>&1; }

WORK=""
LOGIN_PID=""
cleanup(){
    [ -n "$LOGIN_PID" ] && kill "$LOGIN_PID" 2>/dev/null
    [ -n "$WORK" ] && rm -rf "$WORK"
    rm -f "$PID_FILE"
}
trap cleanup EXIT

# Single-instance guard.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    log "another relay (pid $(cat "$PID_FILE")) already running, exiting"
    # Don't let the EXIT trap delete the running relay's pidfile.
    trap - EXIT
    exit 0
fi
echo $$ > "$PID_FILE"

if [ -z "$TOKEN" ]; then
    log "FATAL: no bot token found, cannot relay"
    exit 1
fi
if [ -z "$CHAT_ID" ]; then
    log "FATAL: TELEGRAM_CHAT_ID not set, cannot relay"
    exit 1
fi

WORK=$(mktemp -d)
FIFO="$WORK/stdin"
OUT="$WORK/out"
mkfifo "$FIFO"

log "=== login-relay start (pid $$) ==="

# Defense in depth: auth-monitor only kicks the relay when auth is dead, but if it
# is ever started while auth is healthy, do nothing. Minting a sign-in link nobody
# needs would only train the approve-a-link habit that consent phishing exploits.
if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    log "auth already healthy, relay not needed, exiting without sending a link"
    exit 0
fi

# --- Drain any backlog so we only read the owner's code, not the old /login trigger. ---
drain_offset(){
    local off resp
    off=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
    resp=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=$((off+1))&timeout=0")
    echo "$resp" > "$WORK/drain.json"
    python3 - "$OFFSET_FILE" "$WORK/drain.json" <<'PY'
import sys, json
offfile, respfile = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(respfile))
except Exception:
    sys.exit(0)
mx = None
for u in data.get("result", []):
    mx = u["update_id"]
if mx is not None:
    open(offfile, "w").write(str(mx))
PY
}
drain_offset
log "drained backlog, offset=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"

# --- Start the login flow with controllable stdin + captured stdout. ---
# Open the FIFO read-write on fd 3 (3<>). A plain write-open (3>) blocks until a
# reader exists, but the reader is the login process started on the very next
# line: a classic FIFO deadlock. Opening read-write never blocks and keeps a
# writer held so the login process doesn't see EOF before we feed it the code.
# Closing fd 3 later delivers the EOF.
exec 3<>"$FIFO"
"$CLAUDE" auth login --claudeai < "$FIFO" > "$OUT" 2>&1 &
LOGIN_PID=$!
log "started claude auth login (pid $LOGIN_PID)"

# --- Extract the OAuth URL (strip OSC-8 hyperlink + ANSI escapes, take visible URL). ---
URL=""
for _ in $(seq 1 30); do   # up to ~15s for the URL to appear
    URL=$(perl -0777 -pe 's/\e\][0-9]*;[^\a\e]*(?:\a|\e\\)//g; s/\e\[[0-9;?]*[A-Za-z]//g' "$OUT" 2>/dev/null \
          | grep -oE 'https://claude\.com/cai/oauth/authorize\?[^[:space:]]+' | head -1)
    [ -n "$URL" ] && break
    kill -0 "$LOGIN_PID" 2>/dev/null || break
    sleep 0.5
done

if [ -z "$URL" ] || ! echo "$URL" | grep -q 'state='; then
    log "FATAL: could not extract OAuth URL. Raw out: $(tr -d '\0' < "$OUT" | head -c 300)"
    send "${ASSISTANT}: Recovery hit a snag: I couldn't generate a sign-in link. You'll need to log in on the host machine this time. Details are in login-relay.log."
    exit 1
fi
log "extracted URL (len ${#URL})"

send "🔑 ${ASSISTANT}: Sign-in link to bring me back (opens on your phone, uses your Claude subscription):

$URL

Open it, approve, and Claude will show you a code. Send me JUST that code as your next message. Link and code are one-time. You have about 15 min."

# --- Poll getUpdates for the code (bot token only; no Anthropic auth). ---
CODE=""
deadline=$(( $(date +%s) + CODE_WAIT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    kill -0 "$LOGIN_PID" 2>/dev/null || { log "login process exited before code arrived"; break; }
    off=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
    resp=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=$((off+1))&timeout=20")
    echo "$resp" > "$WORK/poll.json"
    CODE=$(python3 - "$CHAT_ID" "$OFFSET_FILE" "$WORK/poll.json" <<'PY'
import sys, json
chat, offfile, respfile = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(respfile))
except Exception:
    sys.exit(0)
mx = None; found = ""
for u in data.get("result", []):
    mx = u["update_id"]
    m = u.get("message") or {}
    # Only the owner's chat; anyone else messaging the bot is ignored.
    if str((m.get("chat") or {}).get("id")) == chat and (m.get("text") or "").strip():
        t = m["text"].strip()
        if t.lower() not in ("login", "/login"):   # ignore a re-sent trigger
            found = t                               # last real message wins
if mx is not None:
    open(offfile, "w").write(str(mx))
print(found)
PY
)
    [ -n "$CODE" ] && break
done

if [ -z "$CODE" ]; then
    log "timed out waiting for code"
    send "${ASSISTANT}: I didn't get a code in time, so I stopped the sign-in attempt. Send /login again whenever you're ready and I'll mint a fresh link."
    exit 1
fi
log "received code (len ${#CODE})"

# --- Feed the code to the waiting login process, then close stdin. ---
printf '%s\n' "$CODE" >&3
exec 3>&-

# --- Wait for login to finish and verify. ---
wait "$LOGIN_PID" 2>/dev/null
LOGIN_PID=""
sleep 1

if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    log "AUTH RESTORED"
    send "✅ ${ASSISTANT}: I'm back. Auth restored. The watchdog will bring my session up within a minute."
    # Nudge recovery: drop any stale session so the watchdog (auth-gate now open)
    # starts a clean --continue on its next tick.
    tmux kill-session -t "$BRIDGE_SESSION" 2>/dev/null
    exit 0
else
    log "code accepted but auth still not healthy: $("$CLAUDE" auth status 2>&1 | tr -d '\n' | head -c 200)"
    send "${ASSISTANT}: That code didn't take. Send /login again for a fresh link, or log in on the host machine if it keeps failing."
    exit 1
fi
