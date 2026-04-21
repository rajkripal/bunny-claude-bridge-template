#!/usr/bin/env python3
"""PreCompact hook: extract insights to the brain before context compaction.

Fired by Claude Code when the context window fills up. Reads the conversation
from stdin (JSON), extracts decisions/insights/TODOs to the brain.

No-ops silently if CASHEW_HOME isn't configured or the script isn't present.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / "cashew")))
CASHEW_SCRIPT = CASHEW_DIR / "scripts" / "cashew_context.py"
CASHEW_DB = CASHEW_DIR / "data" / "graph.db"


def extract_to_brain(conversation_text: str, session_id: str = "compact") -> bool:
    if not CASHEW_SCRIPT.exists():
        print(f"Warning: brain script not found at {CASHEW_SCRIPT}", file=sys.stderr)
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(conversation_text)
        temp_path = f.name

    try:
        env = os.environ.copy()
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        result = subprocess.run(
            [sys.executable, str(CASHEW_SCRIPT), "extract",
             "--db", str(CASHEW_DB), "--input", temp_path,
             "--session-id", session_id],
            capture_output=True, text=True, timeout=120,
            cwd=str(CASHEW_DIR), env=env,
        )
        if result.returncode == 0:
            print(f"Brain extraction complete: {result.stdout.strip()}", file=sys.stderr)
            return True
        else:
            print(f"Brain extraction failed: {result.stderr.strip()}", file=sys.stderr)
            return False
    finally:
        os.unlink(temp_path)


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    conversation = hook_input.get("conversation", "")
    session_id = hook_input.get("session_id", "pre-compact")
    trigger = hook_input.get("trigger", "unknown")

    if conversation:
        print(f"PreCompact ({trigger}): extracting to brain before compaction", file=sys.stderr)
        extract_to_brain(conversation, session_id)
    else:
        print("PreCompact: no conversation content to extract", file=sys.stderr)

    print("Preserve: all decisions, commitments, TODOs, corrections, and project status changes. "
          "These have been extracted to the brain but should also survive in the summary.")


if __name__ == "__main__":
    main()
