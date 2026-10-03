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
# Safety ceiling for the event-driven wait on the concluding text block to
# flush. This is a MAX bound, not a delay: the wait wakes the instant the
# transcript is written (kqueue file-write event). Override low in tests.
try:
    _FLUSH_TIMEOUT = max(0.05, float(os.environ.get("STOP_HOOK_FLUSH_TIMEOUT", "8")))
except ValueError:
    _FLUSH_TIMEOUT = 8.0
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
# Shipping these to Telegram reads as noise — a 22-byte "No response
# requested." reached the user on 2026-07-02.
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


def _wait_file_write(path: Path, timeout: float) -> bool:
    """Block until `path` is next written, or `timeout` elapses. Event-driven
    via kqueue (macOS/BSD) — wakes the instant the transcript changes, no
    polling, no fixed delay. Returns True if a write fired, False on timeout or
    on a platform without kqueue (caller then does one final read and stops)."""
    if timeout <= 0:
        return False
    try:
        import select
        if not hasattr(select, "kqueue"):
            return False
        fd = os.open(str(path), os.O_RDONLY)
    except (OSError, ImportError):
        return False
    try:
        kq = select.kqueue()
        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE,
        )
        triggered = kq.control([ev], 1, timeout)
        kq.close()
        return bool(triggered)
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


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
        # Thinking blocks are intentionally excluded. They surface the assistant's
        # internal reasoning ("💭 Let me run the script...") which the user
        # reads as verbose Telegram spam. The Stop-hook fallback ships
        # only the user-facing text the assistant produced.
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


def _has_tool_use(msg: dict) -> bool:
    """True if this assistant message contains a tool_use block. My final
    concluding message has none; 'let me do X:' narration messages precede a
    command and do."""
    return bool(_tool_uses(msg))


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


def _tail_after(messages: list[dict], anchor_idx: int) -> list[dict]:
    """Assistant messages between anchor_idx and the next real user message
    (or end of transcript): the reply turn for the inbound at anchor_idx."""
    end_idx = len(messages)
    for i in range(anchor_idx + 1, len(messages)):
        if _is_real_user_message(messages[i]):
            end_idx = i
            break
    return [m for m in messages[anchor_idx + 1 : end_idx] if m.get("type") == "assistant"]


def _turn_already_delivered(
    anchor_idx: int, tail: list[dict], messages: list[dict]
) -> bool:
    """True if the reply for the turn anchored at anchor_idx has already reached
    Telegram: either via a CONFIRMED reply-tool send in its tail, or shipped by
    a prior hook invocation (its exact text is in the delivery log). Used to tell
    two look-alike races apart when the newest inbound has no response yet:

    - Flush race: the prior turn is already delivered, so this Stop belongs to
      the NEWER turn whose concluding text is still flushing to disk. Anchor the
      newest inbound and let the wait loop block for its text. (This is the
      2026-07-08 drop: a consensus answer was lost because the hook anchored on
      the already-answered previous turn and self-satisfied on its reply.)
    - Orphan: the prior turn was never delivered, so this Stop IS that turn and a
      newer inbound merely landed first. Anchor the prior turn and ship it."""
    successful = _successful_reply_tool_ids(messages)
    for am in tail:
        for block in (am.get("message") or {}).get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == TELEGRAM_REPLY_TOOL
                and block.get("id") in successful
            ):
                return True
    parts = [t for t in (_message_text(am).strip() for am in tail) if t]
    if parts:
        chat_id, _ = _extract_channel_info(_message_text(messages[anchor_idx]))
        if _text_already_in_delivery_log(chat_id, "\n\n".join(parts)):
            return True
    return False


def _text_already_in_delivery_log(chat_id: str | None, buf: str) -> bool:
    """Mirror of main()'s send dedupe: True if this exact (chat_id, text) was
    already shipped per the delivery log. Lets a hook-delivered prior turn count
    as delivered without re-shipping it."""
    if not chat_id or not buf or not DELIVERY_LOG.exists():
        return False
    import hashlib
    sig = hashlib.sha1(f"{chat_id}:{buf}".encode()).hexdigest()[:12]
    try:
        lines = DELIVERY_LOG.read_text().splitlines()
    except Exception:
        return False
    for line in reversed(lines[-40:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") == "send_ok" and rec.get("sig") == sig:
            return True
    return False


def _last_user_and_tail_assistant(
    messages: list[dict],
) -> tuple[dict | None, list[dict]]:
    """Find the inbound this Stop is responding to and return it with the
    assistant messages that follow (its reply turn).

    Anchor on the NEWEST real user message by default: that is the inbound the
    just-concluded turn answers. Two races complicate this:

    - Flush race: when Stop fires, the newest inbound's concluding text may not
      have hit disk yet. We must still anchor on that newest inbound (empty tail
      and all) so the wait loop blocks for its text, NOT fall back to the prior,
      already-answered turn. (Falling back was the 2026-07-08 silent drop.)
    - Orphan race: a new inbound can land after the PREVIOUS turn concluded but
      before that turn's Stop fires. Here the newest inbound has no response yet
      AND the prior turn was never delivered, so this Stop is the prior turn.
      Anchor it and ship it, or its reply is orphaned.

    The disambiguator is _turn_already_delivered: only fall back to the prior
    turn when it has NOT already been delivered."""
    newest_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if _is_real_user_message(messages[i]):
            newest_idx = i
            break
    if newest_idx is None:
        return None, []

    newest_tail = _tail_after(messages, newest_idx)
    # Newest inbound already has a response with text: that's the current turn.
    if any(_message_text(m).strip() for m in newest_tail):
        return messages[newest_idx], newest_tail

    # Newest inbound has no text yet. If the PRIOR turn concluded but was never
    # delivered, this Stop is that orphaned turn: anchor and ship it. Otherwise
    # the prior turn is already handled and this is a flush race for the newest
    # turn: keep the newest anchor so the wait loop blocks for its text.
    for i in range(newest_idx - 1, -1, -1):
        if _is_real_user_message(messages[i]):
            prior_tail = _tail_after(messages, i)
            if prior_tail and not _turn_already_delivered(i, prior_tail, messages):
                return messages[i], prior_tail
            break
    return messages[newest_idx], newest_tail


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


def _reply_tool_result_ids(messages: list[dict]) -> tuple[set[str], set[str]]:
    """Return (errored_ids, resulted_ids) for tool_use ids that have an
    explicit tool_result somewhere in the transcript. `resulted_ids` covers
    both success and error results. A tool_use id that is NOT in
    `resulted_ids` at all has no result yet in the transcript we've
    read: it may still be in flight (result not flushed to disk yet) or
    it may be permanently gone (MCP died mid-call, never wrote a result).
    Callers must not treat "not in resulted_ids" as success; see the
    grace-period wait in main() that gives the in-flight case a chance to
    resolve before it's treated as unresolved."""
    errored: set[str] = set()
    resulted: set[str] = set()
    for msg in messages:
        if msg.get("type") != "user":
            continue
        content = (msg.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id", "")
            resulted.add(tid)
            if block.get("is_error"):
                errored.add(tid)
    return errored, resulted


def _successful_reply_tool_ids(messages: list[dict]) -> dict[str, dict]:
    """Map tool_use_id -> {chat_id, sent_at_epoch} for reply-tool calls that
    have a CONFIRMED, non-error tool_result in the transcript. A tool_use
    with no tool_result at all is NOT counted here: it's still unresolved
    (see _reply_tool_result_ids); treating it as sent was the bug that let
    a dead-mid-call MCP produce silent drops with no retry."""
    errored, resulted = _reply_tool_result_ids(messages)
    confirmed = resulted - errored
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
            if tid not in confirmed:
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


def _pending_reply_tool_ids(assistant_msgs: list[dict], messages: list[dict]) -> set[str]:
    """Reply-tool tool_use ids called within `assistant_msgs` that have no
    tool_result anywhere in `messages` yet: neither success nor error."""
    _errored, resulted = _reply_tool_result_ids(messages)
    pending: set[str] = set()
    for am in assistant_msgs:
        content = (am.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == TELEGRAM_REPLY_TOOL
            ):
                tid = block.get("id", "")
                if tid not in resulted:
                    pending.add(tid)
    return pending


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

    # Wait — event-driven, not polled — for the turn to CONCLUDE before we ship.
    # Stop can fire a beat before the final assistant message hits disk; kqueue
    # wakes us the instant it does, so there is no fixed delay. We ship the whole
    # turn's user-facing text: the running "let me do X:" narration AND the final
    # summary. The narration is intentional — it lets the user follow the thought
    # process. The wait exists only to guarantee the concluding block has landed
    # so the delivery isn't truncated mid-action (the 2026-07-04 "...force-delete
    # safely:" failure, where the conclusion hadn't flushed yet). Timeout-bounded.
    import time as _t
    messages: list[dict] = []
    user_msg = None
    assistant_msgs: list[dict] = []
    deadline = None

    def _concluded(tail: list[dict]) -> bool:
        return bool(tail) and bool(_message_text(tail[-1]).strip()) and not _has_tool_use(tail[-1])

    while True:
        messages = _read_transcript(Path(transcript_path))
        user_msg, assistant_msgs = _last_user_and_tail_assistant(messages)
        # Stop waiting once the turn has concluded (final text block flushed),
        # a reply tool was already called, or there is nothing to wait for.
        if _concluded(assistant_msgs):
            break
        # A reply-tool call alone is NOT enough to stop waiting: its
        # tool_result may not have flushed to disk yet (the transcript-flush
        # race this loop exists to dodge). Only stop early here once that
        # tool_use's result: success or error, has actually landed; if it's
        # still pending, keep waiting out the same bounded deadline used for
        # the concluding-text wait below rather than assuming success.
        if (
            assistant_msgs
            and any(TELEGRAM_REPLY_TOOL in _tool_uses(am) for am in assistant_msgs)
            and not _pending_reply_tool_ids(assistant_msgs, messages)
        ):
            break
        if user_msg is None:
            break
        # NOTE: an empty tail is NOT a reason to stop early. During a flush race
        # the newest inbound's response hasn't landed yet; we anchor on it with
        # an empty tail on purpose and must wait (bounded) for its text rather
        # than exit and drop the reply. The deadline below bounds the wait.
        if deadline is None:
            deadline = _t.monotonic() + _FLUSH_TIMEOUT
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            break
        # Blocks until the transcript is next written (or the bound elapses),
        # then re-reads on the next iteration.
        if not _wait_file_write(Path(transcript_path), remaining):
            messages = _read_transcript(Path(transcript_path))
            user_msg, assistant_msgs = _last_user_and_tail_assistant(messages)
            break

    # Ledger first: record successful reply-tool sends no matter how this
    # invocation exits. The queue-sweeper acks inbound messages against these.
    _log_reply_tool_sends(messages)

    if user_msg is None or not assistant_msgs:
        _log("skip", reason="no_anchor_or_tail")
        return 0
    has_text = any(_message_text(am).strip() for am in assistant_msgs)
    if not has_text and not any(
        TELEGRAM_REPLY_TOOL in _tool_uses(am) for am in assistant_msgs
    ):
        _log("skip", reason="no_tail_text")
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

    # Skip only when a reply-tool call in this turn actually SUCCEEDED. A call
    # that errored (e.g. MCP died mid-turn) delivered nothing, and a call
    # whose result never showed up even after the wait loop's grace period
    # (MCP subprocess died before writing ANY tool_result) is equally
    # undelivered: in both cases the hook must ship the turn's concluding
    # text itself or the user sees silence with no retry.
    successful = _successful_reply_tool_ids(messages)
    still_pending = _pending_reply_tool_ids(assistant_msgs, messages)
    if still_pending:
        _log(
            "reply_tool_unresolved", chat_id=chat_id,
            tool_use_ids=sorted(still_pending),
        )
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
    # Ship the whole turn's user-facing text: every text block across the tail
    # assistant messages, in order — the "let me do X:" narration AND the final
    # summary. The user wants the narration; it's how they follow the thought process.
    # The event-wait above guaranteed the concluding block has flushed, so this
    # is the complete turn, not truncated mid-action. (Thinking blocks are still
    # excluded by _message_text — that's private reasoning, not narration.)
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
