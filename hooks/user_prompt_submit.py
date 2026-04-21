#!/usr/bin/env python3
"""UserPromptSubmit hook — instant 👀 reaction on inbound Telegram messages.

Claude's turns can run 5–15 min (tool calls, thinking) before the final
reply ships via Stop hook. From the sender's view this looks like silence
and replies arrive "a message behind." Reacting immediately with 👀 gives
them instant feedback that the message was received and work has started.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

TELEGRAM_CHANNEL_RE = re.compile(
    r'<channel\s+(?P<attrs>[^>]*source="plugin:telegram:telegram"[^>]*)>',
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r'chat_id="(\d+)"')
MSG_ID_RE = re.compile(r'message_id="(\d+)"')

TOKEN_ENV_PATH = Path.home() / ".claude" / "channels" / "telegram" / ".env"
_DEFAULT_LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "bridge-logs"))
REACT_LOG = Path(
    os.environ.get("USER_PROMPT_HOOK_LOG")
    or (_DEFAULT_LOG_DIR / "user-prompt-hook.log")
)


def _log(event: str, **fields) -> None:
    try:
        REACT_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(REACT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _load_bot_token() -> str | None:
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    if not TOKEN_ENV_PATH.exists():
        return None
    for line in TOKEN_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "TELEGRAM_BOT_TOKEN":
            return v.strip().strip('"').strip("'")
    return None


def _react(token: str, chat_id: str, message_id: str, emoji: str = "👀") -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/setMessageReaction"
    payload = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "reaction": [{"type": "emoji", "emoji": emoji}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
            return True, body
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    prompt = payload.get("prompt") or ""
    m = TELEGRAM_CHANNEL_RE.search(prompt)
    if not m:
        return 0
    attrs = m.group("attrs") or ""
    chat = CHAT_ID_RE.search(attrs)
    mid = MSG_ID_RE.search(attrs)
    if not chat or not mid:
        return 0

    chat_id, message_id = chat.group(1), mid.group(1)
    sink = os.environ.get("USER_PROMPT_HOOK_TEST_SINK")
    if sink:
        with open(sink, "a") as f:
            f.write(json.dumps({"chat_id": chat_id, "message_id": message_id, "emoji": "👀"}) + "\n")
        _log("react_test", chat_id=chat_id, message_id=message_id)
        return 0

    token = _load_bot_token()
    if not token:
        _log("skip", reason="no_token")
        return 0

    ok, detail = _react(token, chat_id, message_id)
    if ok:
        _log("react_ok", chat_id=chat_id, message_id=message_id)
    else:
        _log("react_fail", chat_id=chat_id, message_id=message_id, detail=detail[:200])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log("crash", detail=f"{type(e).__name__}: {e}")
        sys.exit(0)
