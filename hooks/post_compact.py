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


def main():
    identity = query_brain(IDENTITY_HINTS)
    recent = query_brain(RECENT_HINTS)

    output_parts = []
    if identity:
        output_parts.append(f"## Identity Context (from brain)\n{identity}")
    if recent:
        output_parts.append(f"## Recent Context (from brain)\n{recent}")

    output_parts.append(
        "## Telegram reminder\n"
        "If the conversation you're resuming came in via Telegram (a <channel "
        "source=\"plugin:telegram:telegram\"> block), your transcript text does "
        "NOT reach the user. Reply via the mcp__plugin_telegram_telegram__reply "
        "tool, passing chat_id from the inbound block. Post-compaction it's easy "
        "to forget — don't."
    )

    if output_parts:
        print("\n\n".join(output_parts))
        print("Brain context restored after compaction.", file=sys.stderr)
    else:
        print("Warning: no brain context available after compaction.", file=sys.stderr)


if __name__ == "__main__":
    main()
