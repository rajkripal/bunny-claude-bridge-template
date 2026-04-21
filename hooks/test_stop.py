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


if __name__ == "__main__":
    unittest.main()
