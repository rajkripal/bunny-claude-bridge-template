#!/bin/bash
# Generate launchd plist files for scheduled jobs.
#
# Reads .env from the repo root for BRIDGE_HOME, BRIDGE_LOG_DIR, BRIDGE_LABEL_PREFIX.
# Writes plists into $BRIDGE_HOME/launchd/jobs/.
#
# Customize the job list at the bottom to match your cron/prompts/.
# Install after generation:
#   for f in launchd/jobs/*.plist; do
#     cp "$f" ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/$(basename "$f")
#   done

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

BRIDGE_DIR="${BRIDGE_HOME:-$REPO_DIR}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/bridge-logs}"
LABEL_PREFIX="${BRIDGE_LABEL_PREFIX:-com.bridge}"

PLIST_DIR="$BRIDGE_DIR/launchd/jobs"
RUNNER="$BRIDGE_DIR/scripts/run-job.sh"
mkdir -p "$PLIST_DIR"

generate_plist() {
    local NAME="$1"
    local HOUR="$2"
    local MINUTE="$3"
    local WEEKDAY="$4"
    local DAY="$5"
    local INTERVAL="$6"
    local NOTIFY="$7"

    local LABEL="${LABEL_PREFIX}.job.${NAME}"
    local FILE="$PLIST_DIR/${LABEL}.plist"

    local NOTIFY_FLAG=""
    [ "$NOTIFY" = "true" ] && NOTIFY_FLAG="<string>--notify</string>"

    if [ -n "$INTERVAL" ]; then
        cat > "$FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
        <string>${NAME}</string>
        ${NOTIFY_FLAG}
    </array>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${NAME}-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
    else
        local CALENDAR=""
        if [ -n "$WEEKDAY" ] && [ -n "$DAY" ]; then
            CALENDAR="<key>Weekday</key><integer>${WEEKDAY}</integer><key>Day</key><integer>${DAY}</integer>"
        elif [ -n "$WEEKDAY" ]; then
            CALENDAR="<key>Weekday</key><integer>${WEEKDAY}</integer>"
        elif [ -n "$DAY" ]; then
            CALENDAR="<key>Day</key><integer>${DAY}</integer>"
        fi

        cat > "$FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
        <string>${NAME}</string>
        ${NOTIFY_FLAG}
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
        ${CALENDAR}
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${NAME}-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
    fi
    echo "Generated: $FILE"
}

generate_multi_weekday_plist() {
    local NAME="$1"
    local HOUR="$2"
    local MINUTE="$3"
    local WEEKDAYS="$4"
    local NOTIFY="$5"

    local LABEL="${LABEL_PREFIX}.job.${NAME}"
    local FILE="$PLIST_DIR/${LABEL}.plist"

    local NOTIFY_FLAG=""
    [ "$NOTIFY" = "true" ] && NOTIFY_FLAG="<string>--notify</string>"

    local CALENDAR_ENTRIES=""
    IFS=',' read -ra DAYS <<< "$WEEKDAYS"
    for day in "${DAYS[@]}"; do
        CALENDAR_ENTRIES="${CALENDAR_ENTRIES}
        <dict>
            <key>Hour</key>
            <integer>${HOUR}</integer>
            <key>Minute</key>
            <integer>${MINUTE}</integer>
            <key>Weekday</key>
            <integer>${day}</integer>
        </dict>"
    done

    cat > "$FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
        <string>${NAME}</string>
        ${NOTIFY_FLAG}
    </array>
    <key>StartCalendarInterval</key>
    <array>${CALENDAR_ENTRIES}
    </array>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${NAME}-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
    echo "Generated: $FILE"
}

# === Jobs — customize or remove as needed. ===
# Each line maps to a prompt in cron/prompts/<NAME>.md.

# BRAIN MAINTENANCE (optional — only useful with a memory backend)
generate_plist "session-extract" "" "" "" "" "7200" "false"            # every 2hrs
generate_multi_weekday_plist "think-cycle" "5" "0" "0,1,2,3,4,5,6" "false"  # 5am daily
generate_plist "sleep-cycle" "" "" "" "" "21600" "false"               # every 6hrs
generate_plist "db-backup" "" "" "" "" "21600" "false"                 # every 6hrs
generate_plist "health-check" "8" "0" "" "" "" "true"                  # 8am daily
generate_plist "brain-metrics" "8" "0" "" "" "" "false"                # 8am daily
generate_plist "declassify" "6" "0" "" "" "" "false"                   # 6am daily

# PRODUCTIVITY
generate_plist "todo-squash" "12" "0" "" "" "" "false"                 # 12pm daily
generate_plist "round-trip" "" "" "" "" "10800" "false"                # every 3hrs
generate_multi_weekday_plist "open-questions" "9" "0" "0,1,2,3,4,5,6" "true"

echo ""
echo "All plists generated in $PLIST_DIR"
echo "To install: for f in $PLIST_DIR/*.plist; do cp \"\$f\" ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/\$(basename \"\$f\"); done"
