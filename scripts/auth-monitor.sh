#!/bin/bash
# Auth monitor: detects a dead Anthropic OAuth session, alerts the owner ONCE,
# and drives the Telegram login-relay recovery, all without depending on the
# assistant's own auth.
#
# Why this exists: OAuth can expire (or fail to refresh) while every process is
# still alive. The watchdog only checks liveness, so without this monitor the
# assistant goes silent for hours while the watchdog log says "restarted
# successfully". This monitor shells out to `claude auth status` (no tokens, no
# API call, a fraction of a second) and alerts via cron/telegram-notify.sh (bot
# token + curl only), so it works when the assistant cannot reach Anthropic at all.
#
# Recovery model: there is exactly ONE Telegram getUpdates reader slot per bot
# token. Normally the bridge's Telegram plugin (bun server.ts) owns it. During a
# confirmed outage the bridge is dead weight (it can't deliver to the stalled
# session), so we STOP it, freeing the slot, and repurpose that one reader for
# recovery: poll for the owner's /login, then hand off to login-relay.sh, which
# mints a sign-in link, relays it to the owner's phone, and reads the code back.
# The watchdog stands down the whole time (its auth-gate), so nothing fights for
# the slot. No second bot needed.
#
# Runs every 60s via launchd (launchd/com.bridge.auth-monitor.plist). macOS only:
# it kicks the relay with launchctl.
#
# Config comes from .env at the repo root (BRIDGE_HOME, BRIDGE_LOG_DIR,
# BRIDGE_SESSION_NAME, BRIDGE_LABEL_PREFIX, ASSISTANT_NAME, TELEGRAM_CHAT_ID,
# optional CLAUDE_BIN). The bot token is read from
# ~/.claude/channels/telegram/.env unless TELEGRAM_BOT_TOKEN is already set.

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

BRIDGE_DIR="${BRIDGE_HOME:-$REPO_DIR}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/bridge-logs}"
SESSION_NAME="${BRIDGE_SESSION_NAME:-bridge}"
ASSISTANT="${ASSISTANT_NAME:-Assistant}"
LABEL_PREFIX="${BRIDGE_LABEL_PREFIX:-com.bridge}"
RELAY_LABEL="${LABEL_PREFIX}.login-relay"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"

STATE_FILE="$LOG_DIR/.auth-monitor-state"
LOG_FILE="$LOG_DIR/auth-monitor.log"
OFFSET_FILE="$LOG_DIR/.relay-offset"
RELAY_PID_FILE="$LOG_DIR/.login-relay.pid"

mkdir -p "$LOG_DIR"
TOKEN="${TELEGRAM_BOT_TOKEN:-$(grep TELEGRAM_BOT_TOKEN "$HOME/.claude/channels/telegram/.env" 2>/dev/null | cut -d= -f2)}"

# Resolve claude to an absolute path. launchd runs with a minimal PATH, so a bare
# `claude` can resolve to nothing, and a missing binary must never read as
# "logged out" (that would page the owner on healthy auth). Set CLAUDE_BIN in
# .env if your install lives somewhere else.
resolve_claude_bin(){
    if [ -n "${CLAUDE_BIN:-}" ]; then echo "$CLAUDE_BIN"; return; fi
    local c
    for c in "$(command -v claude 2>/dev/null)" "$HOME/.local/bin/claude" \
             /opt/homebrew/bin/claude /usr/local/bin/claude; do
        [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
    done
    echo "/opt/homebrew/bin/claude"
}

relay_running(){ [ -f "$RELAY_PID_FILE" ] && kill -0 "$(cat "$RELAY_PID_FILE" 2>/dev/null)" 2>/dev/null; }

# Claude Code's self-update briefly replaces the binary, so a single missing check
# is usually transient. Wait it out before giving up.
for _ in 1 2 3; do
    CLAUDE=$(resolve_claude_bin)
    [ -x "$CLAUDE" ] && break
    sleep 10
done
if [ ! -x "$CLAUDE" ]; then
    # Can't determine auth state. Log loudly but do NOT alert: a missing binary is
    # an install problem, not an auth expiry, and crying wolf trains the owner to
    # ignore the alert that matters.
    echo "[$(date)] ERROR: claude binary not found at $CLAUDE after 30s, cannot check auth" >> "$LOG_FILE"
    exit 1
fi

if [ -z "$CHAT_ID" ]; then
    echo "[$(date)] ERROR: TELEGRAM_CHAT_ID not set in .env, cannot alert or listen for /login" >> "$LOG_FILE"
    exit 1
fi

if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    AUTH_OK=true
else
    AUTH_OK=false
fi

# ---------------------------------------------------------------------------
# Healthy auth: if we were previously in an outage, announce recovery and reset.
# ---------------------------------------------------------------------------
if [ "$AUTH_OK" = true ]; then
    if [ -f "$STATE_FILE" ]; then
        rm -f "$STATE_FILE"
        echo "[$(date)] auth recovered" >> "$LOG_FILE"
        # Only announce here if login-relay didn't already (avoid a double "back online").
        if ! relay_running; then
            bash "$BRIDGE_DIR/cron/telegram-notify.sh" \
                "${ASSISTANT}: Auth is back. I can reach Anthropic again, back online." >/dev/null 2>&1
        fi
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Dead auth. Two-strike rule so a transient blip during a restart doesn't page
# the owner.
# ---------------------------------------------------------------------------
if [ ! -f "$STATE_FILE" ]; then
    echo "alerted=0" > "$STATE_FILE"
    echo "[$(date)] auth check failed (strike 1, not acting yet)" >> "$LOG_FILE"
    # Diagnostics for root-causing expiries (a common trigger is a concurrent
    # headless job failing a token refresh shortly before access-token expiry).
    # Captures what status said, the stored token timing (no secrets), and which
    # claude processes were alive and could have been racing on the refresh.
    # "Claude Code-credentials" is the keychain item Claude Code itself uses.
    {
        echo "  status: $("$CLAUDE" auth status 2>&1 | tr -d '\n' | cut -c1-200)"
        security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null | python3 -c '
import sys, json, time
try:
    o = json.load(sys.stdin).get("claudeAiOauth", {})
    f = lambda v: time.strftime("%m-%d %H:%M:%S", time.localtime(v/1000 if v > 1e12 else v)) if v else "none"
    print("  keychain: expiresAt", f(o.get("expiresAt")), "| refreshExpiresAt", f(o.get("refreshTokenExpiresAt")), "| hasRefresh", bool(o.get("refreshToken")))
except Exception:
    print("  keychain: no readable credentials")'
        echo "  claude procs: $(ps -Ao etime,command= | grep -E '[c]laude( -p| --)' | cut -c1-90 | tr '\n' ';')"
    } >> "$LOG_FILE" 2>&1
    exit 0
fi

# If recovery is already in flight, let it run. Don't poll (that would contend
# for the single reader slot) and don't re-alert.
if relay_running; then
    echo "[$(date)] login-relay in progress (pid $(cat "$RELAY_PID_FILE")), standing by" >> "$LOG_FILE"
    exit 0
fi

alerted=$(grep -o 'alerted=[0-9]*' "$STATE_FILE" | cut -d= -f2)
alerted=${alerted:-0}

# First confirmed outage action: free the reader slot and alert ONCE.
if [ "$alerted" -eq 0 ]; then
    echo "[$(date)] auth DEAD (strike 2), stopping bridge to free the reader slot, alerting once" >> "$LOG_FILE"
    # Stop the dead-weight bridge session so the single getUpdates slot is free
    # for recovery. Safe: the watchdog's auth-gate keeps it from respawning while
    # auth is dead. Kill the tmux session AND explicitly kill the Telegram
    # plugin's poller. tmux kill-session should cascade to it (it's a
    # descendant), but killing it directly guarantees the slot releases so our
    # poll below doesn't 409-conflict with a surviving poller. The poller's argv
    # is just "bun server.ts", so match on its working directory to avoid killing
    # other bun servers (for example the Discord plugin's). Brief settle so
    # Telegram frees the slot.
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null
    for pid in $(pgrep -f "bun server.ts" 2>/dev/null); do
        cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
        case "$cwd" in
            *claude-plugins-official/telegram*) kill "$pid" 2>/dev/null ;;
        esac
    done
    sleep 2
    echo "alerted=1" > "$STATE_FILE"
    bash "$BRIDGE_DIR/cron/telegram-notify.sh" \
"🚨 ${ASSISTANT}: My Anthropic auth expired and I can't reply until it's fixed. Everything you send is queuing unanswered. Restarting won't help.

Whenever you're ready, just send me:  /login

I'll reply within a minute with a one-time sign-in link you can open on your phone (stays on your Claude subscription). I wait for you to ask so the link doesn't expire while you're away.

(This alert is from a separate monitor, not from the assistant, which can't reach Anthropic right now. You'll only get this once.)" >/dev/null 2>&1
fi

# Poll the freed reader slot ONCE for the owner's /login trigger.
if [ -z "$TOKEN" ]; then
    echo "[$(date)] no bot token, cannot poll for /login trigger" >> "$LOG_FILE"
    exit 1
fi

off=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
resp=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=$((off+1))&timeout=15")
echo "$resp" > "$LOG_DIR/.auth-monitor-poll.json"
trigger=$(python3 - "$CHAT_ID" "$OFFSET_FILE" "$LOG_DIR/.auth-monitor-poll.json" <<'PY'
import sys, json
chat, offfile, respfile = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(respfile))
except Exception:
    sys.exit(0)
mx = None; hit = ""
for u in data.get("result", []):
    mx = u["update_id"]
    m = u.get("message") or {}
    # Only the owner's chat counts; anyone can message a bot.
    if str((m.get("chat") or {}).get("id")) == chat:
        t = (m.get("text") or "").strip().lower()
        # Whole-message match only, so a sentence that merely mentions login
        # (e.g. "login later") can't trigger a re-auth. /login is the command;
        # bare "login" still works out of habit.
        if t in ("/login", "login"):
            hit = "yes"
if mx is not None:
    open(offfile, "w").write(str(mx))
print(hit)
PY
)

if [ "$trigger" = "yes" ] && ! relay_running; then
    # Launch login-relay as its OWN launchd job, NOT a backgrounded child.
    # auth-monitor is itself a launchd job; when it exits (every 60s) launchd
    # tears down its entire process group, which SIGKILLs any `nohup ... &` child
    # before it can do anything. A relay launched that way dies instantly without
    # logging a line. A separate launchd job survives auth-monitor's exit.
    echo "[$(date)] owner sent /login, kicking $RELAY_LABEL launchd job" >> "$LOG_FILE"
    launchctl kickstart "gui/$(id -u)/$RELAY_LABEL" 2>/dev/null \
        || launchctl start "$RELAY_LABEL" 2>/dev/null \
        || echo "[$(date)] ERROR: could not kick $RELAY_LABEL job (is launchd/${RELAY_LABEL}.plist loaded?)" >> "$LOG_FILE"
fi

exit 0
