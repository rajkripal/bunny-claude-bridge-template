#!/usr/bin/env python3
"""PostCompact hook: restore identity + recent context after compaction.

Queries the brain for identity and recent work and prints both so they get
injected into the post-compaction session. Also reminds the model about
Telegram reply delivery (see Telegram reminder block below).

No-ops gracefully if CASHEW_HOME isn't configured.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tg_ledger  # noqa: E402

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / "cashew")))
CASHEW_SCRIPT = CASHEW_DIR / "scripts" / "cashew_context.py"

# CUSTOMIZE: same as hooks/init.py — keywords that fetch your identity nodes.
IDENTITY_HINTS = os.environ.get(
    "BRIDGE_IDENTITY_HINTS",
    "<YOUR_IDENTITY_HINTS: e.g. assistant-name identity beliefs operating principles personality>",
)
RECENT_HINTS = "recent projects decisions active work TODO commitments"


def query_brain(hints: str) -> str:
    if not CASHEW_SCRIPT.exists():
        return ""
    env = os.environ.copy()
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        result = subprocess.run(
            [sys.executable, str(CASHEW_SCRIPT), "context", "--hints", hints],
            capture_output=True, text=True, timeout=60,
            cwd=str(CASHEW_DIR), env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"Brain query failed: {result.stderr.strip()}", file=sys.stderr)
        return ""
    except subprocess.TimeoutExpired:
        print("Brain query timed out", file=sys.stderr)
        return ""


def telegram_etiquette_section() -> str:
    """Unanswered inbound messages + the reply-tool reminder.

    Compaction is the #1 moment replies get dropped: the channel tags are
    gone from context and the reply tool's schema is unloaded. Surface both
    facts explicitly so the very next turn re-acks instead of going silent.
    """
    lines = [
        "## Telegram Etiquette (post-compaction)",
        "The telegram reply tool schema is UNLOADED after compaction. Before"
        " replying, load it: ToolSearch"
        ' "select:mcp__plugin_telegram_telegram__reply". Replies go through'
        " the reply tool, never transcript text.",
    ]
    try:
        unanswered = tg_ledger.unanswered_entries()
    except Exception as e:
        lines.append(f"(unanswered-message check failed: {e})")
        return "\n".join(lines)
    if unanswered:
        lines.append("Unanswered messages — ack or answer these NOW:")
        for _, entry, age in unanswered:
            preview = (entry.get("content") or "<attachment>").replace("\n", " ")[:100]
            lines.append(
                f"- #{entry.get('message_id')} from {entry.get('user', '?')}"
                f" ({int(age // 60)}m ago, chat {entry.get('chat_id')}): {preview}"
            )
    else:
        lines.append("No unanswered telegram messages right now.")
    return "\n".join(lines)


def main():
    identity = query_brain(IDENTITY_HINTS)
    recent = query_brain(RECENT_HINTS)

    output_parts = [telegram_etiquette_section()]
    if identity:
        output_parts.append(f"## Identity Context (from brain)\n{identity}")
    if recent:
        output_parts.append(f"## Recent Context (from brain)\n{recent}")

    if output_parts:
        print("\n\n".join(output_parts))
        print("Brain context restored after compaction.", file=sys.stderr)
    else:
        print("Warning: no brain context available after compaction.", file=sys.stderr)


if __name__ == "__main__":
    main()
