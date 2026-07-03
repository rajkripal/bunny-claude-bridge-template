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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tg_ledger  # noqa: E402

TELEGRAM_REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
TELEGRAM_CHANNEL_RE = re.compile(
    r'<channel\s+source="plugin:telegram:[^"]*"(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r'chat_id="([^"]+)"')
MSG_ID_RE = re.compile(r'message_id="([^"]+)"')
DELIVERY_LOG = tg_ledger.DELIVERY_LOG
_log = tg_ledger.log_event

# Harness meta-phrases the assistant emits to satisfy "produce visible output"
# when there is genuinely nothing to say (system notifications, tool loads).
# Shipping these to Telegram reads as noise.
META_TEXT_RE = re.compile(
    r"^(no response (requested|needed)\.?|tool loaded\.?|not applicable\b[^\n]*)$",
    re.IGNORECASE,
)


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
        # Thinking blocks are intentionally excluded. They surface the
        # assistant's internal reasoning trace, not a message to the user.
        # The Stop-hook fallback ships only the user-facing text.
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
    """Find the most recent real user message that carries a Telegram channel
    tag, and return it with all assistant messages that follow.

    Self-triggered wakeups whose "user" prompt is synthetic and has no channel
    tag would be missed if we anchored on the absolute last user message; if
    we anchored on the latest channel-tagged message we'd orphan replies during
    a race. So we walk back from the newest text-bearing assistant message."""
    # Race: a user can send a new telegram message before the PREVIOUS
    # assistant turn's Stop hook fires. If we anchored on "latest channel-
    # tagged user msg", we'd pick the new one and find zero assistant text
    # after it — orphaning the reply to the earlier message.
    #
    # Correct anchor: the user message that preceded the latest text-bearing
    # assistant message. That is, walk back from the end to find the newest
    # assistant-with-text, then anchor on the nearest real user message
    # before it. Prefer channel-tagged if available in that walk-back.
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
        # Fallback for post-compaction transcripts: the summary replaces the
        # channel-tagged history. Anchor on the most recent real user message
        # so we still pick up tail assistant text.
        for i in range(len(messages) - 1, -1, -1):
            if _is_real_user_message(messages[i]):
                anchor_idx = i
                break
    if anchor_idx is None:
        return None, []
    # Cut off at the next real user message so we don't conflate replies to
    # different inbound messages. The anchor's reply is everything between
    # it and the next real user message (or end of transcript).
    end_idx = len(messages)
    for i in range(anchor_idx + 1, len(messages)):
        if _is_real_user_message(messages[i]):
            end_idx = i
            break
    tail = [m for m in messages[anchor_idx + 1 : end_idx] if m.get("type") == "assistant"]
    return messages[anchor_idx], tail


def _last_known_chat_id() -> str | None:
    """Look up the most recent chat_id from the delivery log. Used as a
    post-compaction fallback when the transcript no longer carries a
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
    """Return (chat_id, message_id) from the first telegram channel tag, or (None, None)."""
    m = TELEGRAM_CHANNEL_RE.search(user_text)
    if not m:
        return None, None
    attrs = m.group("attrs") or ""
    chat = CHAT_ID_RE.search(attrs)
    mid = MSG_ID_RE.search(attrs)
    return (chat.group(1) if chat else None, mid.group(1) if mid else None)


_load_bot_token = tg_ledger.load_bot_token
_chunk_for_telegram = tg_ledger.chunk_for_telegram
_send_message = tg_ledger.send_message


def _successful_reply_tool_ids(messages: list[dict]) -> dict[str, dict]:
    """Map tool_use_id -> {chat_id, sent_at_epoch} for reply-tool calls that
    did not error. Only an explicit is_error tool_result counts as failure
    (that's what an MCP death mid-turn produces — e.g. "No such tool
    available"); a missing result is treated as sent, so a transcript flush
    race can't cause a duplicate hook delivery."""
    errored: set[str] = set()
    for msg in messages:
        if msg.get("type") != "user":
            continue
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("is_error")
            ):
                errored.add(block.get("tool_use_id", ""))
    sends: dict[str, dict] = {}
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != TELEGRAM_REPLY_TOOL:
                continue
            tid = block.get("id", "")
            if tid in errored:
                continue
            chat_id = (block.get("input") or {}).get("chat_id")
            epoch = tg_ledger.iso_utc_to_epoch(msg.get("timestamp", "") or "")
            if epoch is None:
                import time as _t
                epoch = _t.time()
            # chat_id may be None (malformed call): still counts for the
            # skip decision, but the ledger logger filters it out.
            sends[tid] = {
                "chat_id": str(chat_id) if chat_id else None,
                "sent_at_epoch": epoch,
            }
    return sends


def _log_reply_tool_sends(messages: list[dict]) -> None:
    """Record successful reply-tool sends in the delivery log (the outbound
    ledger the queue-sweeper acks against). Runs on every hook invocation,
    including textless turns; deduped across runs by tool_use_id."""
    sends = _successful_reply_tool_ids(messages[-300:])
    if not sends:
        return
    seen = tg_ledger.logged_tool_use_ids()
    for tid, info in sends.items():
        if tid in seen or not info.get("chat_id"):
            continue
        _log("reply_send", tool_use_id=tid, **info)


def main() -> int:
    # Cron jobs (run-job.sh) export BUNNY_CRON_JOB=1 before invoking
    # claude -p. Their prompts have no Telegram channel tag, so the
    # post-compaction fallback further down would otherwise treat the
    # cron's last-known chat_id as the destination and ship its output
    # to the user. Skip the hook entirely for cron contexts.
    if os.environ.get("BUNNY_CRON_JOB"):
        _log("skip", reason="cron_job_env")
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    transcript_path = payload.get("transcript_path")
    _log("hook_start", transcript=bool(transcript_path))
    if not transcript_path:
        return 0

    # Stop hook can fire before the final text block is flushed to disk.
    # Retry a few times, looking for a real text-bearing assistant message.
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
            # A turn that already replied via the tool will be skipped below;
            # don't burn 10s waiting for text that doesn't matter.
            if any(TELEGRAM_REPLY_TOOL in _tool_uses(am) for am in assistant_msgs):
                break
        # Only retry if there's an assistant turn in progress (tail exists but
        # no text yet). If no assistant tail at all, waiting won't help.
        if user_msg is None or not assistant_msgs:
            break
        _t.sleep(0.5)

    # Ledger first: record successful reply-tool sends no matter how this
    # invocation exits. The queue-sweeper acks inbound messages against these.
    _log_reply_tool_sends(messages)

    if user_msg is None or not assistant_msgs:
        _log("skip", reason="no_anchor_or_tail", attempts=attempt + 1)
        return 0
    if not had_text and not any(
        TELEGRAM_REPLY_TOOL in _tool_uses(am) for am in assistant_msgs
    ):
        _log("skip", reason="no_tail_text_after_retries", attempts=attempt + 1)
        return 0

    inbound_text = _message_text(user_msg)
    chat_id, reply_msg_id = _extract_channel_info(inbound_text)

    # Quote-reply UX: always point at the latest channel-tagged user msg in the
    # transcript, even if it arrived after the anchor. If the user sent N-1,
    # N, N+1 during one long turn, my coalesced reply should quote N+1 (their
    # latest), not N-1 (the message that started the turn) — that's what feels
    # natural in Telegram. Content still addresses all of them.
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
        # Post-compaction fallback: no channel tag in the anchor, but we can
        # still reach the user if we've talked to them before in this bridge.
        chat_id = _last_known_chat_id()
        reply_msg_id = None
        if not chat_id:
            _log("skip", reason="no_channel_tag")
            return 0
        _log("fallback", reason="post_compaction_chat_id", chat_id=chat_id)

    # Stop fires once at turn-end, not between tool calls. A turn can emit
    # multiple text blocks interleaved with tool use (text → tool → text →
    # tool → final_text); if we only shipped the last one, the user would see
    # only the tail in Telegram while the full reasoning was visible in the
    # terminal. Concatenate all text across tail assistant messages.
    # Skip only when a reply-tool call in this turn actually SUCCEEDED. A call
    # that errored (e.g. MCP died mid-turn) delivered nothing — in that case
    # the hook must ship the turn's text itself or the user sees silence.
    successful = _successful_reply_tool_ids(messages)
    for am in assistant_msgs:
        content = (am.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == TELEGRAM_REPLY_TOOL
                and block.get("id") in successful
            ):
                _log("skip", reason="reply_tool_called", chat_id=chat_id)
                return 0
    parts = [t for t in (_message_text(am).strip() for am in assistant_msgs) if t]
    if not parts:
        _log("skip", reason="no_text", chat_id=chat_id)
        return 0
    buf = "\n\n".join(parts)
    if META_TEXT_RE.match(buf.strip()):
        _log("skip", reason="meta_text", chat_id=chat_id, bytes=len(buf))
        return 0

    # Dedupe: Stop may fire multiple times per turn. Skip if we already shipped
    # this exact text to this chat in the last few delivery-log entries.
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
        return 0  # pure tool-use turn(s); nothing to deliver

    token = _load_bot_token()
    if not token:
        sys.stderr.write(
            "Telegram auto-delivery: TELEGRAM_BOT_TOKEN not found in env or "
            f"{tg_ledger.TOKEN_ENV_PATH}. Cannot deliver this turn's text to Telegram.\n"
        )
        return 2

    chunks = _chunk_for_telegram(buf)
    first = True
    for i, chunk in enumerate(chunks):
        ok, err = _send_message(
            token=token,
            chat_id=chat_id,
            text=chunk,
            reply_to=reply_msg_id if first else None,
        )
        if not ok:
            # Don't fail open: park the unsent remainder in the outbox where
            # the queue-sweeper retries it with backoff until it lands.
            remainder = "\n\n".join(chunks[i:])
            outbox_path = tg_ledger.enqueue_outbox(
                chat_id=chat_id,
                text=remainder,
                reply_to=reply_msg_id if first else None,
                error=err,
            )
            _log(
                "send_fail", chat_id=chat_id, error=err,
                outbox=str(outbox_path) if outbox_path else None,
            )
            if outbox_path:
                sys.stderr.write(
                    f"Telegram auto-delivery failed ({err}); queued to outbox "
                    f"{outbox_path} — the queue-sweeper will retry with backoff.\n"
                )
                return 0
            sys.stderr.write(
                f"Telegram auto-delivery failed: {err}. chat_id={chat_id}. "
                f"Outbox enqueue ALSO failed — this text did not reach the user.\n"
            )
            return 2
        first = False
    _log("send_ok", chat_id=chat_id, reply_to=reply_msg_id, chunks=len(chunks), bytes=sum(len(c) for c in chunks), sig=sig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
