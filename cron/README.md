# Scheduled jobs

Two ways to schedule:

1. **launchd (macOS)** — generate plists with `scripts/generate-plists.sh`,
   then `cp` them to `~/Library/LaunchAgents/` and `launchctl load` each one.
2. **cron (Linux / portable)** — use `crontab.txt` as a starting point.

All jobs ultimately call `claude -p` via `scripts/run-job.sh`, which reads the
prompt from `cron/prompts/<job-name>.md` and pipes the result to Telegram
(if `--notify`) or just logs it.

## Customization

- Delete any prompt you don't want; the corresponding plist/crontab line
  becomes inert (runs a job with no prompt and logs nothing useful).
- Edit `scripts/generate-plists.sh` to add/remove job schedules.
- All logs land in `$BRIDGE_LOG_DIR` (`~/bridge-logs` by default).

## Notes

- `--permission-mode bypassPermissions` — required for headless runs (no
  prompts).
- `--model sonnet` by default — cheaper/faster for routine work; still counts
  against your Max subscription rather than API billing.
- Brain-maintenance jobs (session-extract, think-cycle, sleep-cycle,
  db-backup, health-check, brain-metrics, declassify) assume a cashew-style
  memory backend. Delete them if you're not using one.
