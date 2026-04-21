#!/usr/bin/env python3
"""Stop hook: auto-deliver assistant text to Telegram.

Fires when Claude finishes an assistant turn. If the user turn that triggered
this response came from a Telegram channel AND the assistant turn emitted
user-facing text AND no tool-based telegram reply was sent, this hook POSTs
the text directly to the Telegram bot API using the chat_id and message_id
from the inbound channel tag. The assistant does not need to know or
remember to call the reply tool — delivery becomes deterministic.

Why this exists: a nagging self-correction ("you forgot to reply") fails
open — if the assistant doesn't act on the prompt next turn, the user
never sees the text. Stop hooks run after the turn is complete, so the
assistant cannot "fix" the drop. The real fix is for the hook itself to
deliver.

Behavior:
- Telegram inbound + text + no reply tool call → send via bot API, exit 0.
- Telegram inbound + text + reply tool already called → exit 0, no-op.
- Non-Telegram inbound, or no text → exit 0, no-op.
- Send failure (network, bad token, API error) → exit 2 with stderr error
  so the assistant sees the problem and can retry.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TELEGRAM_REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
TELEGRAM_CHANNEL_RE = re.compile(
    r'<channel\s+source="plugin:telegram:[^"]*"(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r'chat_id="([^"]+)"')
MSG_ID_RE = re.compile(r'message_id="([^"]+)"')
TOKEN_ENV_PATH = Path.home() / ".claude" / "channels" / "telegram" / ".env"
BOT_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_LEN = 4096

_DEFAULT_LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "bridge-logs"))
DELIVERY_LOG = Path(
    os.environ.get("STOP_HOOK_DELIVERY_LOG")
    or (_DEFAULT_LOG_DIR / "stop-hook-delivery.log")
)


def _log(event: str, **fields) -> None:
    try:
        DELIVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        import time as _t
        rec = {"ts": _t.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(DELIVERY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _read_transcript(path: Path) -> list[dict]:
    if not path.exists():
        return []
    messages: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages


def _message_text(msg: dict) -> str:
    m = msg.get("message") or {}
    content = m.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            thought = block.get("thinking", "")
            if thought:
                parts.append(f"thinking: {thought}")
    return "\n\n".join(p for p in parts if p)


def _tool_uses(msg: dict) -> list[str]:
    m = msg.get("message") or {}
    content = m.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            names.append(block.get("name", ""))
    return names


def _is_real_user_message(msg: dict) -> bool:
    """True if this is a human-origin user message, not an API tool_result envelope."""
    if msg.get("type") != "user":
        return False
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    # tool_result-only messages are API bookkeeping, not real user input.
    for block in content:
        if isinstance(block, dict) and block.get("type") != "tool_result":
            return True
    return False


def _last_user_and_tail_assistant(
    messages: list[dict],
) -> tuple[dict | None, list[dict]]:
    """Anchor on the user message that preceded the latest text-bearing
    assistant message. A turn can emit multiple text+tool blocks; we want
    all tail assistant messages after that anchor.

    Race: a user can send a new telegram message before the PREVIOUS
    assistant turn's Stop hook fires. If we anchored on "latest channel-
    tagged user msg", we'd pick the new one and find zero assistant text
    after it — orphaning the reply to the earlier message. Walking back
    from the end avoids this."""
    latest_text_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("type") == "assistant" and _message_text(messages[i]).strip():
            latest_text_assistant_idx = i
            break

    anchor_idx = None
    if latest_text_assistant_idx is not None:
        for i in range(latest_text_assistant_idx - 1, -1, -1):
            if _is_real_user_message(messages[i]):
                anchor_idx = i
                break

    if anchor_idx is None:
        # Fallback for post-compaction transcripts.
        for i in range(len(messages) - 1, -1, -1):
            if _is_real_user_message(messages[i]):
                anchor_idx = i
                break
    if anchor_idx is None:
        return None, []
    end_idx = len(messages)
    for i in range(anchor_idx + 1, len(messages)):
        if _is_real_user_message(messages[i]):
            end_idx = i
            break
    tail = [m for m in messages[anchor_idx + 1 : end_idx] if m.get("type") == "assistant"]
    return messages[anchor_idx], tail


def _last_known_chat_id() -> str | None:
    """Post-compaction fallback when the transcript no longer carries a
    Telegram channel tag."""
    if not DELIVERY_LOG.exists():
        return None
    try:
        lines = DELIVERY_LOG.read_text().splitlines()
    except Exception:
        return None
    for line in reversed(lines[-200:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") == "send_ok" and rec.get("chat_id"):
            return rec["chat_id"]
    return None


def _extract_channel_info(user_text: str) -> tuple[str | None, str | None]:
    m = TELEGRAM_CHANNEL_RE.search(user_text)
    if not m:
        return None, None
    attrs = m.group("attrs") or ""
    chat = CHAT_ID_RE.search(attrs)
    mid = MSG_ID_RE.search(attrs)
    return (chat.group(1) if chat else None, mid.group(1) if mid else None)


def _load_bot_token() -> str | None:
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    if not TOKEN_ENV_PATH.exists():
        return None
    for line in TOKEN_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "TELEGRAM_BOT_TOKEN":
            return v.strip().strip('"').strip("'")
    return None


def _chunk_for_telegram(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_MAX_LEN:
        window = remaining[:TELEGRAM_MAX_LEN]
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > TELEGRAM_MAX_LEN // 2:
                chunks.append(remaining[:idx].rstrip())
                remaining = remaining[idx + len(sep):].lstrip()
                break
        else:
            chunks.append(remaining[:TELEGRAM_MAX_LEN])
            remaining = remaining[TELEGRAM_MAX_LEN:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_to: str | None,
) -> tuple[bool, str]:
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
    sink = os.environ.get("STOP_HOOK_TEST_SINK")
    if sink:
        with open(sink, "a") as f:
            f.write(json.dumps(payload) + "\n")
        return True, ""
    url = f"{BOT_API_BASE}/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if not isinstance(v, str) else v) for k, v in payload.items()}
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            if not parsed.get("ok"):
                return False, parsed.get("description", body)
            return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    transcript_path = payload.get("transcript_path")
    _log("hook_start", transcript=bool(transcript_path))
    if not transcript_path:
        return 0

    import time as _t
    messages: list[dict] = []
    user_msg = None
    assistant_msgs: list[dict] = []
    had_text = False
    attempt = 0
    for attempt in range(20):
        messages = _read_transcript(Path(transcript_path))
        user_msg, assistant_msgs = _last_user_and_tail_assistant(messages)
        if user_msg and assistant_msgs:
            if any(_message_text(am).strip() for am in assistant_msgs):
                had_text = True
                break
        if user_msg is None or not assistant_msgs:
            break
        _t.sleep(0.5)
    if user_msg is None or not assistant_msgs:
        _log("skip", reason="no_anchor_or_tail", attempts=attempt + 1)
        return 0
    if not had_text:
        _log("skip", reason="no_tail_text_after_retries", attempts=attempt + 1)
        return 0

    inbound_text = _message_text(user_msg)
    chat_id, reply_msg_id = _extract_channel_info(inbound_text)

    # Quote-reply UX: always point at the latest channel-tagged user msg in the
    # transcript, even if it arrived after the anchor.
    latest_chat_id = None
    latest_reply_to = None
    for m in reversed(messages):
        if not _is_real_user_message(m):
            continue
        c, r = _extract_channel_info(_message_text(m))
        if c:
            latest_chat_id, latest_reply_to = c, r
            break
    if latest_chat_id:
        chat_id, reply_msg_id = latest_chat_id, latest_reply_to

    if not chat_id:
        chat_id = _last_known_chat_id()
        reply_msg_id = None
        if not chat_id:
            _log("skip", reason="no_channel_tag")
            return 0
        _log("fallback", reason="post_compaction_chat_id", chat_id=chat_id)

    for am in assistant_msgs:
        if TELEGRAM_REPLY_TOOL in _tool_uses(am):
            _log("skip", reason="reply_tool_called", chat_id=chat_id)
            return 0
    parts = [t for t in (_message_text(am).strip() for am in assistant_msgs) if t]
    if not parts:
        _log("skip", reason="no_text", chat_id=chat_id)
        return 0
    buf = "\n\n".join(parts)

    import hashlib
    sig = hashlib.sha1(f"{chat_id}:{buf}".encode()).hexdigest()[:12]
    if DELIVERY_LOG.exists():
        for line in DELIVERY_LOG.read_text().splitlines()[-20:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") == "send_ok" and rec.get("sig") == sig:
                _log("skip", reason="duplicate", chat_id=chat_id, sig=sig)
                return 0
    if not buf.strip():
        _log("skip", reason="no_text", chat_id=chat_id)
        return 0

    token = _load_bot_token()
    if not token:
        sys.stderr.write(
            "Telegram auto-delivery: TELEGRAM_BOT_TOKEN not found in env or "
            f"{TOKEN_ENV_PATH}. Cannot deliver this turn's text to Telegram.\n"
        )
        return 2

    chunks = _chunk_for_telegram(buf)
    first = True
    for chunk in chunks:
        ok, err = _send_message(
            token=token,
            chat_id=chat_id,
            text=chunk,
            reply_to=reply_msg_id if first else None,
        )
        if not ok:
            _log("send_fail", chat_id=chat_id, error=err)
            sys.stderr.write(
                f"Telegram auto-delivery failed: {err}. chat_id={chat_id}. "
                f"The assistant's text did not reach the user.\n"
            )
            return 2
        first = False
    _log("send_ok", chat_id=chat_id, reply_to=reply_msg_id, chunks=len(chunks),
         bytes=sum(len(c) for c in chunks), sig=sig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
