#!/usr/bin/env bash
# set-ack-reaction.sh: set the reaction the Telegram plugin puts on a message the
# moment it arrives (ackReaction). Paired with hooks/user_prompt_submit.py, which
# reacts 👀 when the session actually picks the message up, the sender can tell
# "received, still queued" apart from "being read".
#
# The plugin creates ~/.claude/channels/telegram/access.json when you pair your
# bot, and that file also holds your allowlist, so it can't ship with the repo.
# Run this once after pairing. It changes only ackReaction and keeps everything
# else, writing atomically with the same 0600 permissions the plugin uses. The
# plugin re-reads the file per message, so no restart is needed.
#
# Usage: scripts/set-ack-reaction.sh [emoji]   (default 🫡; pass "" to disable)
set -euo pipefail
EMOJI="${1-🫡}"
FILE="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}/access.json"
if [ ! -f "$FILE" ]; then
  echo "No $FILE yet. Pair your bot first (/telegram:access), then rerun." >&2
  exit 1
fi
python3 - "$FILE" "$EMOJI" <<'PY'
import json, os, sys, tempfile
path, emoji = sys.argv[1], sys.argv[2]
with open(path) as f:
    d = json.load(f)
old = d.get("ackReaction")
d["ackReaction"] = emoji
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".access.")
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"ackReaction: {old!r} -> {emoji!r}")
PY
