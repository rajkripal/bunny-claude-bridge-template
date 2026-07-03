#!/usr/bin/env python3
"""Tests for tg_ledger.py + queue-sweeper.py + tg-ack.

Everything runs in a temp sandbox via env overrides:
  TG_CHANNEL_DIR / STOP_HOOK_DELIVERY_LOG / TG_SWEEPER_LOG / TG_SWEEPER_LOCK
  TG_SWEEPER_NUDGE_SINK   — nudges append to a file instead of tmux
  TG_SWEEPER_ASSUME_ALIVE — skip the ps check
  STOP_HOOK_TEST_SINK     — outbound sends append to a file instead of the bot API
  STOP_HOOK_TEST_FAIL     — force outbound sends to fail
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
SWEEPER = HOOKS_DIR.parent / "scripts" / "queue-sweeper.py"
TG_ACK = HOOKS_DIR.parent / "scripts" / "tg-ack"


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local_ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.channel = self.root / "telegram"
        self.queue = self.channel / "queue"
        self.queue.mkdir(parents=True)
        self.delivery_log = self.root / "delivery.log"
        self.sweep_log = self.root / "sweeper.log"
        self.nudge_sink = self.root / "nudges.txt"
        self.send_sink = self.root / "sends.jsonl"
        self.env = {
            **os.environ,
            "TG_CHANNEL_DIR": str(self.channel),
            "STOP_HOOK_DELIVERY_LOG": str(self.delivery_log),
            "TG_SWEEPER_LOG": str(self.sweep_log),
            "TG_SWEEPER_LOCK": str(self.root / "sweep.lock"),
            "TG_SWEEPER_NUDGE_SINK": str(self.nudge_sink),
            "TG_SWEEPER_ASSUME_ALIVE": "1",
            "STOP_HOOK_TEST_SINK": str(self.send_sink),
            "TELEGRAM_BOT_TOKEN": "T",
        }
        self.env.pop("STOP_HOOK_TEST_FAIL", None)

    def add_queue_entry(self, message_id: str, arrived_epoch: float, *,
                        chat_id: str = "111", content: str = "hello",
                        phase: str = "1", extra: dict | None = None) -> Path:
        entry = {
            "chat_id": chat_id,
            "message_id": message_id,
            "user": "alice",
            "user_id": chat_id,
            "ts": iso_utc(arrived_epoch),
            "content": content,
            "queued_at": iso_utc(arrived_epoch),
            "phase": phase,
            **(extra or {}),
        }
        stamp = iso_utc(arrived_epoch).replace(":", "-").replace(".", "-")
        path = self.queue / f"{stamp}-{chat_id}-{message_id}.json"
        path.write_text(json.dumps(entry))
        return path

    def log_outbound(self, event: str, chat_id: str, epoch: float, **fields) -> None:
        rec = {"ts": local_ts(epoch), "event": event, "chat_id": chat_id, **fields}
        with open(self.delivery_log, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def run_sweeper(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SWEEPER)], env=self.env,
            capture_output=True, text=True, timeout=60,
        )

    def entry(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def nudges(self) -> list[str]:
        if not self.nudge_sink.exists():
            return []
        return self.nudge_sink.read_text().splitlines()

    def sends(self) -> list[dict]:
        if not self.send_sink.exists():
            return []
        return [json.loads(l) for l in self.send_sink.read_text().splitlines()]


class TestAnsweredDetection(unittest.TestCase):
    def test_outbound_after_arrival_marks_answered(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 600)
        sb.log_outbound("send_ok", "111", now - 300)
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "2")
        self.assertEqual(sb.nudges(), [])

    def test_outbound_before_arrival_does_not_answer(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 300)
        sb.log_outbound("send_ok", "111", now - 600)
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "1")
        self.assertEqual(len(sb.nudges()), 1)
        self.assertIn("#10", sb.nudges()[0])

    def test_reply_send_uses_precise_epoch(self):
        # Reply-tool send at T, message arrives at T+60: NOT answered even
        # though the ledger record was written later (turn end).
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 240)
        sb.log_outbound("reply_send", "111", now - 10, sent_at_epoch=now - 300)
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "1")

    def test_other_chat_outbound_does_not_answer(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 600, chat_id="111")
        sb.log_outbound("send_ok", "222", now - 100)
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "1")

    def test_legacy_reply_tool_called_skip_counts(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 600)
        sb.log_outbound("skip", "111", now - 100, reason="reply_tool_called")
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "2")


class TestNudgeEscalation(unittest.TestCase):
    def test_stale_unanswered_backfilled_not_nudged(self):
        sb = Sandbox()
        p = sb.add_queue_entry("10", time.time() - 30 * 3600)
        sb.run_sweeper()
        entry = sb.entry(p)
        self.assertEqual(entry["phase"], "2")
        self.assertTrue(entry["assumed"])
        self.assertEqual(sb.nudges(), [])

    def test_young_message_not_nudged(self):
        sb = Sandbox()
        sb.add_queue_entry("10", time.time() - 60)
        sb.run_sweeper()
        self.assertEqual(sb.nudges(), [])

    def test_nudge_count_persisted_and_spaced(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 400)
        sb.run_sweeper()
        self.assertEqual(len(sb.nudges()), 1)
        self.assertEqual(sb.entry(p)["nudge_count"], 1)
        # Immediate re-run: within 10-minute spacing, no second nudge.
        sb.run_sweeper()
        self.assertEqual(len(sb.nudges()), 1)

    def test_gives_up_after_max_nudges_and_notifies_once(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry(
            "10", now - 7200,
            extra={"nudge_count": 3, "last_nudge_at": now - 3600},
        )
        p2 = sb.add_queue_entry(
            "11", now - 7200, content="second",
            extra={"nudge_count": 3, "last_nudge_at": now - 3600},
        )
        sb.run_sweeper()
        self.assertEqual(sb.entry(p)["phase"], "9")
        self.assertEqual(sb.entry(p2)["phase"], "9")
        # Give-up notice sent to the chat, but only once (1/hour global cap).
        notices = [s for s in sb.sends() if "dropped your message" in s["text"]]
        self.assertEqual(len(notices), 1)


class TestOutbox(unittest.TestCase):
    def test_outbox_retry_success_acks_queue(self):
        sb = Sandbox()
        now = time.time()
        qp = sb.add_queue_entry("10", now - 600)
        outbox = sb.channel / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "x.json").write_text(json.dumps({
            "chat_id": "111", "text": "delayed reply", "reply_to": "10",
            "enqueued_at": now - 300, "retry_count": 1,
            "last_attempt_at": now - 300, "last_error": "boom",
        }))
        sb.run_sweeper()
        self.assertEqual([s["text"] for s in sb.sends()], ["delayed reply"])
        self.assertFalse((outbox / "x.json").exists())
        # The outbox_sent ledger event answers the pending queue entry.
        sb.run_sweeper()
        self.assertEqual(sb.entry(qp)["phase"], "2")

    def test_outbox_backoff_defers_retry(self):
        sb = Sandbox()
        now = time.time()
        outbox = sb.channel / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "x.json").write_text(json.dumps({
            "chat_id": "111", "text": "delayed", "reply_to": None,
            "enqueued_at": now, "retry_count": 3,
            "last_attempt_at": now - 60, "last_error": "boom",
        }))
        sb.run_sweeper()  # backoff for retry 3 is 600s; 60s elapsed -> defer
        self.assertEqual(sb.sends(), [])
        self.assertTrue((outbox / "x.json").exists())

    def test_outbox_moves_to_failed_after_max_retries(self):
        sb = Sandbox()
        now = time.time()
        outbox = sb.channel / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "x.json").write_text(json.dumps({
            "chat_id": "111", "text": "doomed", "reply_to": None,
            "enqueued_at": now, "retry_count": 4,
            "last_attempt_at": now - 3600, "last_error": "boom",
        }))
        sb.env["STOP_HOOK_TEST_FAIL"] = "1"
        sb.run_sweeper()
        self.assertFalse((outbox / "x.json").exists())
        self.assertTrue((outbox / "failed" / "x.json").exists())
        nudge = "\n".join(sb.nudges())
        self.assertIn("failed 5 times", nudge)


class TestTgAck(unittest.TestCase):
    def test_ack_marks_phase_3_and_stops_nudges(self):
        sb = Sandbox()
        now = time.time()
        p = sb.add_queue_entry("10", now - 400)
        r = subprocess.run(
            [sys.executable, str(TG_ACK), "10"], env=sb.env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sb.entry(p)["phase"], "3")
        sb.run_sweeper()
        self.assertEqual(sb.nudges(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
