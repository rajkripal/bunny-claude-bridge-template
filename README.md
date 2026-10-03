# claude-bridge template

A template for running a persistent, always-on Claude Code assistant connected
to Telegram and/or Discord, with optional [cashew](https://github.com/rajkripal/cashew) brain
memory and a library of scheduled jobs (launchd on macOS, cron elsewhere).

This is the sanitized skeleton of a working setup. Clone it, fill in your
identity and credentials, and you have the bones of a persistent AI assistant.

## What you get

- **CLAUDE.md template** — the identity/instructions file. A framework, not a
  soul. You write the soul.
- **Hooks** — `SessionStart`, `PreCompact`, `PostCompact`, `UserPromptSubmit`,
  `Stop`. The `Stop` hook is the important one: it auto-delivers assistant
  text to Telegram so your replies never silently drop.
- **Watchdog** — launchd-driven keepalive that restarts the `claude` tmux
  session if it dies or the Telegram MCP subprocess crashes.
- **Auth recovery** (`scripts/auth-monitor.sh` + `scripts/login-relay.sh`):
  notices when your Claude sign-in has died and lets you log back in from
  your phone over Telegram. See [Auth recovery from your phone](#auth-recovery-from-your-phone).
- **Job runner** — `scripts/run-job.sh` runs a prompt file through `claude -p`
  headless, logs the output, optionally pings you on Telegram.
- **Launchd plist generator** — `scripts/generate-plists.sh` stamps out jobs.
- **Cron prompt library** — reusable patterns (session-extract, think-cycle,
  sleep-cycle, health-check, todo-squash, round-trip, declassify, brain-metrics).
- **Telegram + Discord notify scripts** — cron-friendly message senders.

## Prerequisites

- macOS (launchd) or Linux (use cron instead of launchd)
- [Claude Code CLI](https://claude.com/product/claude-code) on a Max plan
  (headless `claude -p` uses your subscription, not API billing)
- Python 3.10+
- `tmux` (for the persistent session)
- `bun` (required by the Claude Code Telegram/Discord channel plugins)
- *Optional:* cashew brain (any persistent-memory backend works; the hooks
  assume cashew's CLI)
- A Telegram bot token (talk to @BotFather) and your chat_id, and/or a
  Discord bot token + your user ID

## Setup

1. **Clone and rename.**
   ```bash
   git clone <this-repo> ~/my-bridge
   cd ~/my-bridge
   ```

2. **Fill in the env.** Copy `env.example` → `.env` at the repo root and set:
   - `BRIDGE_HOME` — absolute path to this checkout
   - `BRIDGE_LOG_DIR` — where logs go (e.g. `~/bridge-logs`)
   - `CASHEW_HOME` — absolute path to your cashew checkout (leave blank if
     not using a brain; hooks will no-op)
   - `TELEGRAM_CHAT_ID` — your Telegram chat_id (the watchdog uses this to
     tell you it restarted)
   - `DISCORD_USER_ID` — your Discord user ID (if using Discord)

3. **Set the Telegram token** where hooks/scripts expect it:
   ```bash
   mkdir -p ~/.claude/channels/telegram
   echo "TELEGRAM_BOT_TOKEN=<your_token>" > ~/.claude/channels/telegram/.env
   ```
   (Same pattern for `~/.claude/channels/discord/.env` with `DISCORD_BOT_TOKEN`.)

4. **Edit `CLAUDE.md`.** Replace every `<FILL IN>` with your own identity,
   context, operating principles. This file *is* the assistant's personality;
   don't skip it.

5. **Install hooks.** Copy `.claude/settings.json` paths to wherever Claude
   Code loads project settings from for your checkout (the paths in the
   shipped file use `${BRIDGE_HOME}` — expand them to absolutes before use,
   or set per-project settings after cloning).

6. **Generate + load launchd jobs** (macOS):
   ```bash
   bash scripts/generate-plists.sh
   for f in launchd/jobs/com.bridge.job.*.plist; do
     cp "$f" ~/Library/LaunchAgents/
     launchctl load ~/Library/LaunchAgents/$(basename "$f")
   done
   cp launchd/com.bridge.claude-bridge.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.bridge.claude-bridge.plist
   ```

   Also load the auth-recovery pair (recommended, see
   [Auth recovery from your phone](#auth-recovery-from-your-phone)).

   Or on Linux: `crontab cron/crontab.txt`.

7. **Start the session.** The watchdog will do this automatically once loaded,
   but to boot it manually:
   ```bash
   tmux new-session -d -s bridge "claude --channels plugin:telegram@claude-plugins-official"
   ```

8. **Pair your Telegram user.** DM your bot once, then run
   `/telegram:access` inside the tmux session to approve yourself.

## Auth recovery from your phone

### Why it exists

The bridge runs on your Claude subscription through Claude Code's OAuth
sign-in. That sign-in can expire or fail to refresh while every process stays
alive. From the outside the bridge looks healthy: tmux is up, `claude` is
running, the watchdog is happy. But nothing you send gets answered, and a
restart can't fix it. Without something checking auth specifically, you only
find out hours later.

### How it works

1. **Detect.** `scripts/auth-monitor.sh` runs every 60s and asks
   `claude auth status` (no tokens spent, no API call). A single failure is
   only logged, with some diagnostics. Two failures in a row count as an
   outage, so a blip during a restart doesn't page you.
2. **Alert once.** On a confirmed outage the monitor stops the bridge session
   and sends you one Telegram message through the bot token and `curl`, which
   needs no Anthropic auth. The watchdog stands down while auth is dead
   instead of restarting into it. You get this alert once per outage.
3. **Send `/login`.** Whenever you're ready, send `/login` (bare `login` also
   works) to your bot. It must be the whole message, so "login later" or
   "I can't login" never start a sign-in. Messages from any other chat are
   ignored. The link isn't minted until you ask, so it can't expire while
   you're away.
4. **Open the link.** The monitor kicks `scripts/login-relay.sh`, which runs
   `claude auth login --claudeai` on the host and sends you the sign-in link.
   Open it on your phone and approve.
5. **Paste the code back.** Claude shows you a code. Send just that code as
   your next message to the bot. The relay feeds it to the waiting login
   process and checks `claude auth status`. You have about 15 minutes.
6. **Auto-restart.** On success the relay tells you so and drops the stale
   session. The watchdog's auth check now passes, so it brings the bridge back
   with `--continue` within a minute.

Why the session gets stopped: a Telegram bot has exactly one `getUpdates`
reader. Normally the Telegram plugin owns it. During an outage the plugin is
useless anyway, so the monitor stops it and reuses that one reader to listen
for `/login` and your code. No second bot is needed.

### Setup (macOS)

1. Make sure `.env` has `TELEGRAM_CHAT_ID` set and the bot token is in
   `~/.claude/channels/telegram/.env` (both are part of the normal setup). If
   `claude` isn't on launchd's PATH, set `CLAUDE_BIN` in `.env` to its absolute
   path.
2. Edit the `/PATH/TO/...` placeholders in
   `launchd/com.bridge.auth-monitor.plist` and
   `launchd/com.bridge.login-relay.plist`. If you changed
   `BRIDGE_LABEL_PREFIX`, change both labels to match: the monitor kicks the
   job named `${BRIDGE_LABEL_PREFIX}.login-relay`.
3. Load both:
   ```bash
   for p in auth-monitor login-relay; do
     cp launchd/com.bridge.$p.plist ~/Library/LaunchAgents/
     launchctl load ~/Library/LaunchAgents/com.bridge.$p.plist
   done
   ```
   The monitor runs every 60s. The relay has no schedule: loading it only
   registers it so the monitor can start it on demand. It is a separate job on
   purpose. A background child of the monitor would be killed by launchd the
   moment the monitor's run ends.
4. Optional: `bash scripts/test_auth_recovery.sh` runs the whole flow against
   fakes (no real auth, tmux, launchd or Telegram).

Logs: `auth-monitor.log` and `login-relay.log` in `BRIDGE_LOG_DIR`. If recovery
fails, you can always run `claude auth login` on the host itself.

### Security

- **What crosses Telegram.** Bot chats are not end-to-end encrypted, so
  assume Telegram can read them. What you send back is a single-use
  authorization code that is only valid for a few minutes and is bound by
  PKCE to the login process waiting on your machine. It is useless to anyone
  without that process. Your access and refresh tokens are never sent over
  Telegram; they stay on the host.
- **Consent phishing.** Only approve a Claude sign-in link that you just
  requested by sending `/login` to your own bot, and only send the resulting
  code back to that same bot. Anyone can generate a Claude login link for
  their own machine and send it to you. If you approve it and hand them the
  code, they get a session on your account. An unexpected sign-in link is a
  red flag, not a prompt. For the same reason the relay refuses to mint a
  link when auth is already healthy.

## Directory layout

```
CLAUDE.md              Identity & instructions template (fill in)
env.example            Env-var template — copy to .env
.claude/settings.json  Hook registration
hooks/                 Python hooks for Claude Code lifecycle
scripts/               Watchdog, auth monitor + login relay, job runner, plist generator
cron/                  Telegram/Discord notify helpers + prompt library
  prompts/             Reusable prompts for scheduled jobs
launchd/               plist templates (generated by scripts/generate-plists.sh)
```

## Things to customize

- [ ] `CLAUDE.md` — every `<FILL IN>` block
- [ ] `env.example` → `.env` — paths, chat IDs, log dir
- [ ] `hooks/init.py` and `hooks/post_compact.py` — the identity hints passed
      to the brain query (the placeholder says `<YOUR_IDENTITY_HINTS>`)
- [ ] `cron/prompts/` — delete prompts you don't want; edit the rest to match
      your tools and accounts
- [ ] `scripts/generate-plists.sh` — schedules and which jobs exist
- [ ] `scripts/watchdog.sh` — the label, session name, restart message
- [ ] `launchd/com.bridge.auth-monitor.plist` and `com.bridge.login-relay.plist`:
      paths, and labels if you changed the prefix
- [ ] Label prefix: the template uses `com.bridge.*` for launchd labels;
      change to your own prefix if you'd like

## Notes on the hooks

- **`stop.py`** is the load-bearing one. Its job is: at end of every assistant
  turn, if the user message came from Telegram and the assistant produced text
  but didn't explicitly call the reply tool, POST the text to the Telegram
  Bot API. Without this, long tool-heavy turns silently drop their final text.
- **`user_prompt_submit.py`** reacts with 👀 on inbound Telegram messages so
  the sender gets instant feedback while the model thinks.
- **`pre_compact.py` / `post_compact.py`** — brain integration. Extract to
  memory before compaction, restore identity/context after. Gracefully no-op
  if `CASHEW_HOME` is not set.

## Security reminders

- Never commit `.env` or anything in `~/.claude/channels/*/.env`.
- `TELEGRAM_BOT_TOKEN` grants full control of your bot — rotate if leaked.
- The Telegram/Discord channel plugins have their own access-control skills
  (`/telegram:access`, `/discord:access`) — use them.
