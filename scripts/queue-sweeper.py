#!/usr/bin/env python3
"""Queue sweeper: delivery guarantees for the Telegram bridge.

Runs every 2 minutes via launchd (com.bridge.queue-sweeper). Three passes:

1. Ack pass    — inbound queue entries with a later successful outbound to the
                 same chat get marked phase 2 (answered). Pure bookkeeping.
2. Nudge pass  — entries still unanswered after NUDGE_AFTER_S get a one-line
                 nudge typed into the live claude tmux session (the session has
                 the context; the sweeper only points at message ids). Nudges
                 escalate 3m/10m/30m, max 3, state stored in the entry itself.
                 After the third ignored nudge the entry goes phase 9 and the
                 sender gets a single give-up notice (global 1/hour cap).
3. Outbox pass — failed outbound sends (queued by stop.py) are retried with
                 exponential backoff (5s/25s/2m/10m), max 5 attempts, then
                 moved to outbox/failed/ and surfaced via nudge.

No LLM, no context, no decisions about content. Deterministic switchboard only.
Actions are logged to ~/bridge-logs/queue-sweeper.log (or $BRIDGE_LOG_DIR) (JSONL).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import tg_ledger  # noqa: E402

SWEEP_LOG = Path(
    os.environ.get("TG_SWEEPER_LOG")
    or (Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "bridge-logs")) / "queue-sweeper.log")
)
LOCK_PATH = Path(os.environ.get("TG_SWEEPER_LOCK") or "/tmp/bridge-queue-sweeper.lock")
TMUX_SESSION = (
    os.environ.get("TG_SWEEPER_TMUX_SESSION")
    or os.environ.get("BRIDGE_SESSION_NAME")
    or "bridge"
)

NUDGE_AFTER_S = 180
NUDGE_SPACING_S = [600, 1800]  # wait before 2nd and 3rd nudge
MAX_NUDGES = 3
# Entries older than this are assumed handled (phase 2, assumed=true) instead
# of nudged: the outbound ledger may not reach back that far, and a nudge
# about a day-old message is noise, not accountability.
STALE_S = 24 * 3600
GIVEUP_NOTICE_COOLDOWN_S = 3600
OUTBOX_BACKOFF_S = [5, 25, 120, 600, 600]
OUTBOX_MAX_RETRIES = 5
PREVIEW_LEN = 80
TG_ACK = str(Path(__file__).resolve().parent / "tg-ack")


def log(event: str, **fields) -> None:
    try:
        SWEEP_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(SWEEP_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def acquire_lock() -> bool:
    """Single-flight guard. Stale locks (>10 min) are stolen."""
    try:
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > 600:
            LOCK_PATH.unlink()
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True  # a broken lock must not disable delivery guarantees


def release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def claude_session_alive() -> bool:
    if os.environ.get("TG_SWEEPER_ASSUME_ALIVE"):
        return True
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
        return f"--name {TMUX_SESSION}" in out
    except Exception:
        return False


def type_into_session(text: str) -> bool:
    """Type one line into the live claude session. The nudge becomes a queued
    user message; claude answers it with full context on its next turn."""
    sink = os.environ.get("TG_SWEEPER_NUDGE_SINK")
    if sink:
        with open(sink, "a") as f:
            f.write(text + "\n")
        return True
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION, "-l", "--", text],
            check=True, timeout=10, capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"],
            check=True, timeout=10, capture_output=True,
        )
        return True
    except Exception as e:
        log("nudge_type_failed", error=f"{type(e).__name__}: {e}")
        return False


def preview(entry: dict) -> str:
    content = (entry.get("content") or "").replace("\n", " ").strip()
    if not content and entry.get("attachment"):
        content = f"<{entry['attachment'].get('kind', 'attachment')}>"
    if not content and entry.get("image_path"):
        content = "<photo>"
    return content[:PREVIEW_LEN] + ("…" if len(content) > PREVIEW_LEN else "")


def age_str(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def recent_giveup_notice(now: float) -> bool:
    if not SWEEP_LOG.exists():
        return False
    try:
        lines = SWEEP_LOG.read_text().splitlines()[-500:]
    except Exception:
        return False
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") == "giveup_notice":
            epoch = tg_ledger._local_ts_to_epoch(rec.get("ts", ""))
            return epoch is not None and now - epoch < GIVEUP_NOTICE_COOLDOWN_S
    return False


def ack_pass() -> int:
    marked = 0
    for path, entry in tg_ledger.answered_phase1_entries():
        entry["phase"] = "2"
        entry["answered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if tg_ledger.rewrite_entry(path, entry):
            marked += 1
    if marked:
        log("acked", count=marked)
    return marked


def nudge_pass(now: float) -> None:
    due: list[tuple[Path, dict, float]] = []
    gave_up: list[tuple[Path, dict]] = []
    stale = 0
    for path, entry, age in tg_ledger.unanswered_entries(now):
        if age < NUDGE_AFTER_S:
            continue
        if age > STALE_S:
            entry["phase"] = "2"
            entry["answered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            entry["assumed"] = True
            if tg_ledger.rewrite_entry(path, entry):
                stale += 1
            continue
        count = int(entry.get("nudge_count", 0))
        last = float(entry.get("last_nudge_at", 0))
        if count >= MAX_NUDGES:
            gave_up.append((path, entry))
            continue
        if count > 0 and now - last < NUDGE_SPACING_S[min(count - 1, len(NUDGE_SPACING_S) - 1)]:
            continue
        due.append((path, entry, age))

    if stale:
        log("stale_backfill", count=stale)

    if due:
        if not claude_session_alive():
            # Watchdog owns dead-session recovery; nudges land after restart.
            log("nudge_deferred", reason="claude_session_dead", count=len(due))
        else:
            lines = "; ".join(
                f"#{e.get('message_id')} {e.get('user', '?')} {age_str(age)} ago: \"{preview(e)}\""
                for _, e, age in due
            )
            ids = " ".join(str(e.get("message_id")) for _, e, _ in due)
            text = (
                f"[queue-sweeper] Unanswered telegram message(s): {lines}. "
                f"Reply or ack via the telegram reply tool (load it via ToolSearch if unloaded). "
                f"If no reply is warranted: {TG_ACK} {ids}"
            )
            if type_into_session(text):
                for path, entry, _ in due:
                    entry["nudge_count"] = int(entry.get("nudge_count", 0)) + 1
                    entry["last_nudge_at"] = now
                    tg_ledger.rewrite_entry(path, entry)
                log("nudged", ids=ids, counts=[int(e.get("nudge_count", 0)) for _, e, _ in due])

    for path, entry in gave_up:
        entry["phase"] = "9"
        tg_ledger.rewrite_entry(path, entry)
        log("gave_up", message_id=entry.get("message_id"), chat_id=entry.get("chat_id"))
        if not recent_giveup_notice(now):
            token = tg_ledger.load_bot_token()
            if token:
                ok, err = tg_ledger.send_message(
                    token,
                    str(entry.get("chat_id")),
                    f"⚠️ I seem to have dropped your message #{entry.get('message_id')} "
                    f"(\"{preview(entry)}\"). Please re-send if it still needs an answer.",
                    str(entry.get("message_id")),
                )
                log("giveup_notice", message_id=entry.get("message_id"), ok=ok, error=err or None)


def outbox_pass(now: float) -> None:
    if not tg_ledger.OUTBOX_DIR.is_dir():
        return
    for path in sorted(tg_ledger.OUTBOX_DIR.glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except Exception:
            continue
        retries = int(entry.get("retry_count", 0))
        backoff = OUTBOX_BACKOFF_S[min(retries, len(OUTBOX_BACKOFF_S) - 1)]
        if now - float(entry.get("last_attempt_at", 0)) < backoff:
            continue
        token = tg_ledger.load_bot_token()
        if not token:
            log("outbox_no_token")
            return
        ok = True
        err = ""
        first = True
        for chunk in tg_ledger.chunk_for_telegram(entry.get("text", "")):
            ok, err = tg_ledger.send_message(
                token, str(entry["chat_id"]), chunk,
                entry.get("reply_to") if first else None,
            )
            if not ok:
                break
            first = False
        if ok:
            path.unlink(missing_ok=True)
            # Logged to the DELIVERY log so the ack pass counts it as outbound.
            tg_ledger.log_event("outbox_sent", chat_id=str(entry["chat_id"]), retries=retries)
            log("outbox_sent", chat_id=entry["chat_id"], retries=retries)
        else:
            entry["retry_count"] = retries + 1
            entry["last_attempt_at"] = now
            entry["last_error"] = err
            if entry["retry_count"] >= OUTBOX_MAX_RETRIES:
                tg_ledger.OUTBOX_FAILED_DIR.mkdir(parents=True, exist_ok=True)
                failed_path = tg_ledger.OUTBOX_FAILED_DIR / path.name
                tg_ledger.rewrite_entry(path, entry)
                shutil.move(str(path), str(failed_path))
                log("outbox_failed", chat_id=entry["chat_id"], error=err, moved_to=str(failed_path))
                if claude_session_alive():
                    type_into_session(
                        f"[queue-sweeper] Outbound telegram send to chat {entry['chat_id']} "
                        f"failed {OUTBOX_MAX_RETRIES} times ({err}). Payload parked at "
                        f"{failed_path}. Investigate and resend manually."
                    )
            else:
                tg_ledger.rewrite_entry(path, entry)
                log("outbox_retry_failed", chat_id=entry["chat_id"],
                    retry_count=entry["retry_count"], error=err)


def main() -> int:
    if not acquire_lock():
        return 0
    try:
        now = time.time()
        ack_pass()
        nudge_pass(now)
        outbox_pass(now)
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
