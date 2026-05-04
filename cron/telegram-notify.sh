#!/bin/bash
# Send a message to the owner via Telegram.
# Usage:
#   echo "message" | bash telegram-notify.sh
#   bash telegram-notify.sh "direct message"
#
# Reads .env from the repo root for TELEGRAM_CHAT_ID.
# Reads ~/.claude/channels/telegram/.env for TELEGRAM_BOT_TOKEN.

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

TELEGRAM_TOKEN=$(cat ~/.claude/channels/telegram/.env 2>/dev/null | grep TELEGRAM_BOT_TOKEN | cut -d= -f2)

if [ -z "$TELEGRAM_TOKEN" ]; then
    echo "Error: TELEGRAM_BOT_TOKEN not set" >&2
    exit 1
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "Error: TELEGRAM_CHAT_ID not set in .env" >&2
    exit 1
fi

if [ -n "$1" ]; then
    MESSAGE="$1"
else
    MESSAGE=$(cat)
fi

MESSAGE="${MESSAGE:0:4090}"

# Send as plain text. Markdown / MarkdownV2 parse modes silently reject
# messages with unbalanced *, _, ( or unescaped $ — and the failure is
# invisible if curl errors are sent to /dev/null. Plain text is more
# robust for cron-generated output.
RESPONSE=$(curl -s --max-time 30 "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" 2>&1)

# Surface failures. Caller (run-job.sh) appends our stdout to the job log,
# so a non-ok response will land there for postmortem instead of vanishing.
if ! echo "$RESPONSE" | grep -q '"ok":true'; then
    echo "[telegram-notify] SEND FAILED: $RESPONSE" >&2
    exit 2
fi
echo "[telegram-notify] sent ok ($(echo -n "$MESSAGE" | wc -c) chars)"
