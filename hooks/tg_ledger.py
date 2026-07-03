#!/usr/bin/env python3
"""Shared Telegram delivery-ledger library.

Used by three consumers:
  - hooks/stop.py            — logs outbound sends, enqueues failed sends to the outbox
  - scripts/queue-sweeper.py — marks inbound queue entries answered, nudges the live
                               session about silence, retries the outbox with backoff
  - hooks/post_compact.py    — lists unanswered messages right after compaction

The ledger has no state of its own. "Answered" is computed by cross-referencing
two artifacts that already exist on disk:
  - inbound:  ~/.claude/channels/telegram/queue/*.json   (written by the plugin MCP)
  - outbound: ~/bridge-logs/stop-hook-delivery.log (or $BRIDGE_LOG_DIR) (written by stop.py)

A queue entry counts as answered when ANY successful outbound send to the same
chat happened at-or-after the entry's arrival time. That is deliberately
conversation-level, not message-level: one reply can answer a burst of three
messages. Reply-tool sends use the transcript timestamp of the tool call, so a
message that arrives mid-turn AFTER the reply was sent stays unanswered.

Queue entry phases (plugin writes "1" and never reads the field back):
  "1" — awaiting answer
  "2" — answered (sweeper observed a later outbound to the chat)
  "3" — acked manually via scripts/tg-ack (no reply warranted)
  "9" — gave up (3 nudges ignored; give-up notice sent)
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CHANNEL_DIR = Path(
    os.environ.get("TG_CHANNEL_DIR") or (Path.home() / ".claude" / "channels" / "telegram")
)
QUEUE_DIR = CHANNEL_DIR / "queue"
OUTBOX_DIR = CHANNEL_DIR / "outbox"
OUTBOX_FAILED_DIR = OUTBOX_DIR / "failed"
TOKEN_ENV_PATH = CHANNEL_DIR / ".env"
_LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "bridge-logs"))
DELIVERY_LOG = Path(
    os.environ.get("STOP_HOOK_DELIVERY_LOG")
    or (_LOG_DIR / "stop-hook-delivery.log")
)

BOT_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_LEN = 4096

# Delivery-log events that prove text reached a chat. Values name the field
# holding the precise send epoch; None means "parse the record's local ts".
OUTBOUND_EVENTS = {
    "send_ok": None,            # stop-hook direct send, ts = turn end
    "reply_send": "sent_at_epoch",  # reply tool call, ts = transcript timestamp
    "outbox_sent": None,        # sweeper retry succeeded
    "reply_tool_called": None,  # legacy skip records (pre-ledger), turn end
}


def log_event(event: str, **fields) -> None:
    """Append one JSONL record to the delivery log. Never raises."""
    try:
        DELIVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(DELIVERY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def load_bot_token() -> str | None:
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


def chunk_for_telegram(text: str) -> list[str]:
    """Split text into <=4096-char chunks on paragraph / sentence boundaries."""
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


def send_message(token: str, chat_id: str, text: str, reply_to: str | None) -> tuple[bool, str]:
    """POST to sendMessage. Returns (ok, error_or_empty)."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_parameters"] = {
            "message_id": int(reply_to),
            "allow_sending_without_reply": True,
        }
    # Test hook: record payload to a file instead of hitting the network.
    sink = os.environ.get("STOP_HOOK_TEST_SINK")
    if sink:
        if os.environ.get("STOP_HOOK_TEST_FAIL"):
            return False, "test-forced failure"
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


def _local_ts_to_epoch(ts: str) -> float | None:
    """Delivery-log ts fields are naive local time (time.strftime)."""
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def iso_utc_to_epoch(ts: str) -> float | None:
    """Queue-entry timestamps are ISO-8601 UTC with Z suffix."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def outbound_epochs_by_chat(max_lines: int = 5000) -> dict[str, list[float]]:
    """Parse the delivery log into {chat_id: [send epochs]}."""
    if not DELIVERY_LOG.exists():
        return {}
    result: dict[str, list[float]] = {}
    try:
        lines = DELIVERY_LOG.read_text().splitlines()[-max_lines:]
    except Exception:
        return {}
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        event = rec.get("event")
        # Legacy records logged reply-tool usage as a skip with a reason field.
        if event == "skip" and rec.get("reason") == "reply_tool_called":
            event = "reply_tool_called"
        if event not in OUTBOUND_EVENTS:
            continue
        chat_id = rec.get("chat_id")
        if not chat_id:
            continue
        epoch_field = OUTBOUND_EVENTS[event]
        epoch = rec.get(epoch_field) if epoch_field else None
        if not isinstance(epoch, (int, float)):
            epoch = _local_ts_to_epoch(rec.get("ts", ""))
        if epoch is None:
            continue
        result.setdefault(str(chat_id), []).append(float(epoch))
    return result


def logged_tool_use_ids(max_lines: int = 2000) -> set[str]:
    """tool_use ids already recorded as reply_send events (dedupe across hook runs)."""
    if not DELIVERY_LOG.exists():
        return set()
    ids: set[str] = set()
    try:
        lines = DELIVERY_LOG.read_text().splitlines()[-max_lines:]
    except Exception:
        return ids
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") == "reply_send" and rec.get("tool_use_id"):
            ids.add(rec["tool_use_id"])
    return ids


def queue_entries() -> list[tuple[Path, dict]]:
    """All parseable inbound queue entries, oldest first (filenames sort by time)."""
    if not QUEUE_DIR.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        try:
            out.append((path, json.loads(path.read_text())))
        except Exception:
            continue
    return out


def entry_epoch(entry: dict) -> float | None:
    return iso_utc_to_epoch(entry.get("queued_at") or entry.get("ts") or "")


def unanswered_entries(now: float | None = None) -> list[tuple[Path, dict, float]]:
    """Phase-1 entries with no successful outbound to their chat at-or-after arrival.

    Returns (path, entry, age_seconds) oldest first. Does not mutate anything.
    """
    now = now if now is not None else time.time()
    outbound = outbound_epochs_by_chat()
    result: list[tuple[Path, dict, float]] = []
    for path, entry in queue_entries():
        if str(entry.get("phase", "1")) != "1":
            continue
        arrived = entry_epoch(entry)
        if arrived is None:
            continue
        sends = outbound.get(str(entry.get("chat_id", "")), [])
        if any(s >= arrived for s in sends):
            continue
        result.append((path, entry, now - arrived))
    return result


def answered_phase1_entries() -> list[tuple[Path, dict]]:
    """Phase-1 entries that HAVE a later outbound (ready to be marked phase 2)."""
    outbound = outbound_epochs_by_chat()
    result: list[tuple[Path, dict]] = []
    for path, entry in queue_entries():
        if str(entry.get("phase", "1")) != "1":
            continue
        arrived = entry_epoch(entry)
        if arrived is None:
            continue
        sends = outbound.get(str(entry.get("chat_id", "")), [])
        if any(s >= arrived for s in sends):
            result.append((path, entry))
    return result


def rewrite_entry(path: Path, entry: dict) -> bool:
    """Atomic tmp+rename rewrite, mirroring the plugin's own write pattern."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(entry, indent=2) + "\n")
        tmp.rename(path)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def enqueue_outbox(chat_id: str, text: str, reply_to: str | None, error: str) -> Path | None:
    """Persist a failed outbound send for the sweeper to retry."""
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")
        path = OUTBOX_DIR / f"{stamp}-{chat_id}.json"
        entry = {
            "chat_id": str(chat_id),
            "text": text,
            "reply_to": reply_to,
            "enqueued_at": now,
            "retry_count": 0,
            "last_attempt_at": now,
            "last_error": error,
        }
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry, indent=2) + "\n")
        tmp.rename(path)
        return path
    except Exception:
        return None
