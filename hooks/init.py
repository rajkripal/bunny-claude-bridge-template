#!/usr/bin/env python3
"""SessionStart hook: load brain context on session start/resume.

Reads CASHEW_HOME from env (or defaults to ~/cashew). If the brain script
isn't present, this hook no-ops silently — use this template without a
memory backend just as easily.

The hints string below is the IDENTITY query — keywords that should fetch
the assistant's persona/beliefs/operating-principles nodes. Customize it
to match whatever you tagged your identity nodes with.
"""

import os
import subprocess
import sys
from pathlib import Path

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / "cashew")))
CASHEW_SCRIPT = CASHEW_DIR / "scripts" / "cashew_context.py"

# CUSTOMIZE: keywords that fetch your assistant's identity nodes from the brain.
IDENTITY_HINTS = os.environ.get(
    "BRIDGE_IDENTITY_HINTS",
    "<YOUR_IDENTITY_HINTS: e.g. assistant-name identity beliefs operating principles personality>",
)


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
        return result.stdout.strip() if result.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""


def main():
    identity = query_brain(IDENTITY_HINTS)
    if identity:
        print(f"## Brain Context (auto-loaded on init)\n{identity}")
        print("Brain context loaded on session start.", file=sys.stderr)
    else:
        print("No brain context available. Running without memory.", file=sys.stderr)


if __name__ == "__main__":
    main()
