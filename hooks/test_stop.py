#!/usr/bin/env python3
"""Tests for hooks/stop.py — the Telegram auto-delivery hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).parent / "stop.py"


def _jsonl(messages: list[dict]) -> str:
    return "\n".join(json.dumps(m) for m in messages)


def _user(content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(content) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _tool_use(name: str) -> dict:
    return {"type": "tool_use", "name": name, "input": {}, "id": "t"}


def run_hook(transcript: list[dict], *, token: str | None = "T", sink: str | None = None) -> tuple[int, str, list[dict]]:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(_jsonl(transcript))
        path = f.name
    if sink is None:
        sink_f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        sink_f.close()
        sink = sink_f.name
    log_f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    log_f.close()
    env = {
        **os.environ,
        "STOP_HOOK_TEST_SINK": sink,
        "STOP_HOOK_DELIVERY_LOG": log_f.name,
    }
    if token is None:
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env["HOME"] = tempfile.mkdtemp()
    else:
        env["TELEGRAM_BOT_TOKEN"] = token
    payload = json.dumps({"transcript_path": path})
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload, text=True, capture_output=True, timeout=15, env=env,
    )
    sent = []
    if Path(sink).exists():
        for line in Path(sink).read_text().splitlines():
            if line.strip():
                sent.append(json.loads(line))
    return result.returncode, result.stderr, sent


CHANNEL_MSG = '<channel source="plugin:telegram:telegram" chat_id="123" message_id="42">hi</channel>'


class StopHookTests(unittest.TestCase):
    def test_auto_delivers_text_to_telegram(self):
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([_text("here is my answer")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "123")
        self.assertEqual(sent[0]["text"], "here is my answer")
        self.assertEqual(sent[0]["reply_parameters"]["message_id"], 42)

    def test_skips_when_reply_tool_already_called(self):
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([
                _text("sending now"),
                _tool_use("mcp__plugin_telegram_telegram__reply"),
            ]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_skips_when_reply_tool_in_later_turn(self):
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([_tool_use("Bash")]),
            _assistant([_text("answer"),
                        _tool_use("mcp__plugin_telegram_telegram__reply")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_skips_non_telegram_inbound(self):
        code, _, sent = run_hook([
            _user("plain terminal message"),
            _assistant([_text("ok")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_skips_pure_tool_turn(self):
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([_tool_use("Bash")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_concatenates_all_tail_assistant_text(self):
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([_text("part one")]),
            _assistant([_tool_use("Bash")]),
            _assistant([_text("part two")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "part one\n\npart two")

    def test_string_content_user_message(self):
        code, _, sent = run_hook([
            {"type": "user", "message": {"role": "user", "content": CHANNEL_MSG}},
            _assistant([_text("reply")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)

    def test_missing_token_errors(self):
        code, stderr, sent = run_hook(
            [_user(CHANNEL_MSG), _assistant([_text("hi")])],
            token=None,
        )
        self.assertEqual(code, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", stderr)
        self.assertEqual(sent, [])

    def test_chunks_long_text(self):
        long = ("word " * 1200).strip()
        code, _, sent = run_hook([
            _user(CHANNEL_MSG),
            _assistant([_text(long)]),
        ])
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(sent), 2)
        self.assertIn("reply_parameters", sent[0])
        self.assertNotIn("reply_parameters", sent[1])
        for s in sent:
            self.assertLessEqual(len(s["text"]), 4096)

    def test_no_assistant_turn(self):
        code, _, sent = run_hook([_user(CHANNEL_MSG)])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_missing_transcript_path(self):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="{}", text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)

    def test_empty_stdin(self):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="", text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)


class LedgerAndOutboxTests(unittest.TestCase):
    """New behaviors: reply_send ledger events, error-aware skip, meta-text
    filter, and outbox fallback on send failure."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sink = self.root / "sends.jsonl"
        self.log = self.root / "delivery.log"
        self.channel = self.root / "telegram"
        self.env = {
            **os.environ,
            "STOP_HOOK_TEST_SINK": str(self.sink),
            "STOP_HOOK_DELIVERY_LOG": str(self.log),
            "TG_CHANNEL_DIR": str(self.channel),
            "TELEGRAM_BOT_TOKEN": "T",
        }
        self.env.pop("STOP_HOOK_TEST_FAIL", None)
        self.env.pop("BUNNY_CRON_JOB", None)

    def run_hook_env(self, transcript: list[dict]) -> int:
        path = self.root / "transcript.jsonl"
        path.write_text(_jsonl(transcript))
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"transcript_path": str(path)}),
            text=True, capture_output=True, timeout=15, env=self.env,
        )
        return result.returncode

    def sent(self) -> list[dict]:
        if not self.sink.exists():
            return []
        return [json.loads(l) for l in self.sink.read_text().splitlines() if l.strip()]

    def log_events(self, event: str) -> list[dict]:
        if not self.log.exists():
            return []
        recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [r for r in recs if r.get("event") == event]

    def _reply_tool_use(self, tid: str = "tu1", chat_id: str = "123") -> dict:
        return {
            "type": "tool_use", "id": tid, "input": {"chat_id": chat_id, "text": "hi"},
            "name": "mcp__plugin_telegram_telegram__reply",
        }

    def _tool_result(self, tid: str, *, is_error: bool = False) -> dict:
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
             "content": "err" if is_error else "sent"},
        ]}}

    def test_successful_reply_logged_as_reply_send(self):
        code = self.run_hook_env([
            _user(CHANNEL_MSG),
            {"type": "assistant", "timestamp": "2026-07-02T08:00:00.000Z",
             "message": {"role": "assistant", "content": [self._reply_tool_use()]}},
            self._tool_result("tu1"),
            _assistant([_text("done")]),
        ])
        self.assertEqual(code, 0)
        events = self.log_events("reply_send")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["chat_id"], "123")
        self.assertEqual(events[0]["tool_use_id"], "tu1")
        # Epoch comes from the transcript timestamp, not hook runtime.
        from datetime import datetime, timezone
        expected = datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(events[0]["sent_at_epoch"], expected)
        # And the hook skipped its own delivery (reply tool succeeded).
        self.assertEqual(self.sent(), [])

    def test_errored_reply_tool_does_not_skip_delivery(self):
        code = self.run_hook_env([
            _user(CHANNEL_MSG),
            {"type": "assistant", "message": {"role": "assistant", "content": [
                self._reply_tool_use("tu9"),
            ]}},
            self._tool_result("tu9", is_error=True),
            _assistant([_text("the actual answer")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self.log_events("reply_send"), [])
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "the actual answer")

    def test_reply_send_deduped_across_runs(self):
        transcript = [
            _user(CHANNEL_MSG),
            {"type": "assistant", "timestamp": "2026-07-02T08:00:00.000Z",
             "message": {"role": "assistant", "content": [self._reply_tool_use()]}},
            self._tool_result("tu1"),
            _assistant([_text("done")]),
        ]
        self.run_hook_env(transcript)
        self.run_hook_env(transcript)
        self.assertEqual(len(self.log_events("reply_send")), 1)

    def test_meta_text_not_shipped(self):
        code = self.run_hook_env([
            _user(CHANNEL_MSG),
            _assistant([_text("No response requested.")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(), [])
        skips = self.log_events("skip")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["reason"], "meta_text")

    def test_send_failure_queues_outbox_and_exits_zero(self):
        self.env["STOP_HOOK_TEST_FAIL"] = "1"
        code = self.run_hook_env([
            _user(CHANNEL_MSG),
            _assistant([_text("important answer")]),
        ])
        self.assertEqual(code, 0)
        outbox = list((self.channel / "outbox").glob("*.json"))
        self.assertEqual(len(outbox), 1)
        entry = json.loads(outbox[0].read_text())
        self.assertEqual(entry["chat_id"], "123")
        self.assertEqual(entry["text"], "important answer")
        self.assertEqual(entry["reply_to"], "42")


if __name__ == "__main__":
    unittest.main()
