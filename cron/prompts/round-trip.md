Run the round-trip tracker against the most recent Claude Code session file.

## Step 1: Find the session file
```bash
ls -t ~/.claude/projects/*/*.jsonl 2>/dev/null | head -1
```

## Step 2: Run tracker
```bash
cd "${CASHEW_HOME}/.." && python3 scripts/round-trip-tracker.py --from-jsonl "<session_file>"
```
(Adjust the path to wherever your round-trip-tracker.py lives.)

## Step 3: Report
```bash
python3 scripts/round-trip-tracker.py --report
```

Report the summary stats briefly. If nothing new was tracked, reply: TRACKER_OK
