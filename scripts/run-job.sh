#!/bin/bash
# Generic headless job runner: runs `claude -p` with a prompt file, logs the
# output, optionally pings Telegram.
#
# Usage: run-job.sh <job-name> [--notify] [--prompt-file path] [--inline "prompt"]
#
# Reads .env from the repo root if present (for BRIDGE_HOME, BRIDGE_LOG_DIR,
# HEADLESS_MODEL). The default prompt location is $BRIDGE_HOME/cron/prompts/<job-name>.md.

set -e

JOB_NAME="$1"
shift

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

BRIDGE_DIR="${BRIDGE_HOME:-$REPO_DIR}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/bridge-logs}"
MODEL="${HEADLESS_MODEL:-sonnet}"

NOTIFY=false
PROMPT_FILE=""
INLINE_PROMPT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --notify) NOTIFY=true; shift ;;
        --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
        --inline) INLINE_PROMPT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

mkdir -p "$LOG_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export KMP_DUPLICATE_LIB_OK="TRUE"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] Starting job: $JOB_NAME" >> "$LOG_DIR/$JOB_NAME.log"

if [ -n "$INLINE_PROMPT" ]; then
    PROMPT="$INLINE_PROMPT"
elif [ -n "$PROMPT_FILE" ]; then
    PROMPT=$(cat "$PROMPT_FILE")
else
    PROMPT=$(cat "$BRIDGE_DIR/cron/prompts/$JOB_NAME.md" 2>/dev/null || echo "Run job: $JOB_NAME")
fi

# Run claude -p with a hard 10-minute timeout. A stuck `claude -p` otherwise
# pins a PID indefinitely and blocks the next scheduled run's assumptions.
OUTPUT=$(cd "$BRIDGE_DIR" && perl -e 'alarm shift; exec @ARGV' 600 claude -p "$PROMPT" --permission-mode bypassPermissions --model "$MODEL" 2>&1) || true
RC=$?
if [ "$RC" = "142" ]; then
    OUTPUT="${OUTPUT}
[run-job.sh: killed after 10min timeout — job hung]"
fi

echo "$OUTPUT" >> "$LOG_DIR/$JOB_NAME.log"
echo "[$TIMESTAMP] Job complete: $JOB_NAME" >> "$LOG_DIR/$JOB_NAME.log"
echo "---" >> "$LOG_DIR/$JOB_NAME.log"

if [ "$NOTIFY" = true ] && [ -n "$OUTPUT" ]; then
    MESSAGE="${JOB_NAME}
${OUTPUT}"
    bash "$BRIDGE_DIR/cron/telegram-notify.sh" "$MESSAGE"
fi
