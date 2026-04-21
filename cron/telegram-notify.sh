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

curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "text=${MESSAGE}" \
    > /dev/null 2>&1
