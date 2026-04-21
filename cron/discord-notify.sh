#!/bin/bash
# Send a DM to the owner via Discord.
# Usage:
#   echo "message" | bash discord-notify.sh
#   bash discord-notify.sh "direct message"
#
# Reads .env from the repo root for DISCORD_USER_ID.
# Reads ~/.claude/channels/discord/.env for DISCORD_BOT_TOKEN.

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

ENV_FILE="$HOME/.claude/channels/discord/.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

if [ -z "$DISCORD_BOT_TOKEN" ]; then
    echo "Error: DISCORD_BOT_TOKEN not set" >&2
    exit 1
fi
if [ -z "$DISCORD_USER_ID" ]; then
    echo "Error: DISCORD_USER_ID not set in .env" >&2
    exit 1
fi

# Get or create DM channel
DM_CHANNEL=$(curl -s -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"recipient_id\": \"$DISCORD_USER_ID\"}" \
    https://discord.com/api/v10/users/@me/channels | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

if [ -z "$DM_CHANNEL" ]; then
    echo "Error: Could not create DM channel" >&2
    exit 1
fi

if [ -n "$1" ]; then
    MESSAGE="$1"
else
    MESSAGE=$(cat)
fi

MESSAGE="${MESSAGE:0:1990}"

curl -s -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}))" "$MESSAGE" 2>/dev/null || echo "{\"content\": \"Cron job completed. Check logs.\"}")" \
    "https://discord.com/api/v10/channels/$DM_CHANNEL/messages" > /dev/null
