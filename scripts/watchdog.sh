#!/bin/bash
# Watchdog: ensures the Claude Code Telegram bridge is always running.
# Called by launchd every 60 seconds (see launchd/com.bridge.claude-bridge.plist).
#
# Config: customize BRIDGE_DIR / SESSION_NAME / add-dir paths below.
# If .env exists at the repo root, it's sourced for BRIDGE_HOME, BRIDGE_LOG_DIR,
# BRIDGE_SESSION_NAME, ASSISTANT_NAME, TELEGRAM_CHAT_ID.

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

BRIDGE_DIR="${BRIDGE_HOME:-$REPO_DIR}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/bridge-logs}"
SESSION_NAME="${BRIDGE_SESSION_NAME:-bridge}"
ASSISTANT="${ASSISTANT_NAME:-Assistant}"
SESSION_MODEL="${SESSION_MODEL:-claude-opus-4-7}"
TELEGRAM_TOKEN=$(cat ~/.claude/channels/telegram/.env 2>/dev/null | grep TELEGRAM_BOT_TOKEN | cut -d= -f2)

mkdir -p "$LOG_DIR"

# Health check: tmux session alive + claude process alive + telegram MCP subprocess alive.
# The MCP subprocess (bun server.ts) can crash silently while claude keeps running,
# leaving the bridge unable to send/receive Telegram. Treat missing MCP as a restart trigger.
REASON=""
if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    REASON="tmux session missing"
else
    # pane_pid is the shell (zsh) that launched claude, not claude itself.
    # When claude exits cleanly, zsh stays alive and pane_pid appears healthy
    # while the assistant is actually dead. Detect the real claude process via
    # its --name flag (unique per bridge session). pgrep -f doesn't reliably
    # see the claude binary on macOS, so scan ps output directly.
    CLAUDE_PID=$(ps -Ao pid,command= | awk -v n="--name $SESSION_NAME" '$0 ~ n && !/awk/ {print $1; exit}')
    if [ -z "$CLAUDE_PID" ]; then
        REASON="claude process missing"
    elif ! pgrep -f "claude-plugins-official/telegram.*start" > /dev/null 2>&1; then
        # Grace period: MCP child may not have spawned yet. On a cold Mac
        # boot, MCP can take longer than 90s to register, so use 300s.
        # Real MCP deaths observed in practice all had claude up >400s.
        ETIME=$(ps -o etime= -p "$CLAUDE_PID" 2>/dev/null | awk '{print $1}')
        ETIME_SEC=$(echo "$ETIME" | awk -F'[-:]' '{
            if (NF==4) print $1*86400 + $2*3600 + $3*60 + $4;
            else if (NF==3) print $1*3600 + $2*60 + $3;
            else if (NF==2) print $1*60 + $2;
            else print 0
        }')
        if [ "${ETIME_SEC:-0}" -gt 300 ]; then
            REASON="telegram MCP subprocess missing (claude up ${ETIME_SEC}s)"
        fi
    fi
fi

if [ -z "$REASON" ]; then
    exit 0
fi

echo "[$(date)] Watchdog: restarting — $REASON" >> "$LOG_DIR/watchdog.log"

tmux kill-session -t "$SESSION_NAME" 2>/dev/null

tmux new-session -d -s "$SESSION_NAME"
sleep 1

# --continue resumes the prior conversation. Falls back to cold start on first
# boot or fresh install. Append claude stderr to claude-stderr.log so the NEXT
# MCP death leaves a diagnosable trail.
#
# CUSTOMIZE: add --add-dir entries for any workspace directories the assistant
# needs access to (e.g. cashew, scripts). Remove --dangerously-skip-permissions
# if you want to be prompted.
ADD_DIRS=""
[ -n "$CASHEW_HOME" ] && ADD_DIRS="$ADD_DIRS --add-dir $CASHEW_HOME"
# ADD_DIRS="$ADD_DIRS --add-dir /path/to/your/workspace"

tmux send-keys -t "$SESSION_NAME" "cd $BRIDGE_DIR && export TELEGRAM_BOT_ENABLED=1 && (claude --continue --model $SESSION_MODEL --channels plugin:telegram@claude-plugins-official --name ${SESSION_NAME} --dangerously-skip-permissions $ADD_DIRS || claude --model $SESSION_MODEL --channels plugin:telegram@claude-plugins-official --name ${SESSION_NAME} --dangerously-skip-permissions $ADD_DIRS) 2>> $LOG_DIR/claude-stderr.log" Enter

# Accept the bypass-permissions prompt (if present).
sleep 10
tmux send-keys -t "$SESSION_NAME" Down
sleep 0.5
tmux send-keys -t "$SESSION_NAME" Enter

echo "[$(date)] Watchdog: restarted successfully" >> "$LOG_DIR/watchdog.log"

# Notify the user on Telegram that we restarted.
if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${ASSISTANT}: Watchdog restarted (${REASON}). Back online." \
        > /dev/null 2>&1
fi
