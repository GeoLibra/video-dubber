#!/bin/sh
# L0 Resident Shell Guard for video-dubber
# Independent resident guard depending on no active user session.
# If L1 heartbeat timestamp is stale (> 2h) or missing, spins up an emergency patrol via cron_sweep_jobs.py --emergency.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve project root by walking upward until finding repo markers (.git and skills)
dir="$SCRIPT_DIR"
PROJECT_ROOT=""
while [ "$dir" != "/" ] && [ -n "$dir" ]; do
    if [ -d "$dir/.git" ] && [ -d "$dir/skills" ]; then
        PROJECT_ROOT="$dir"
        break
    fi
    dir="$(dirname "$dir")"
done

if [ -z "$PROJECT_ROOT" ]; then
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
fi

DEFAULT_JOBS_DIR="$PROJECT_ROOT/output"
DEFAULT_HEARTBEAT_FILE="$DEFAULT_JOBS_DIR/.l1_heartbeat"
DEFAULT_LOG_FILE="$DEFAULT_JOBS_DIR/logs/l0_guard.log"

# Default configuration (can be overridden by env vars or CLI flags)
L1_HEARTBEAT_FILE="${L1_HEARTBEAT_FILE:-$DEFAULT_HEARTBEAT_FILE}"
JOBS_DIR="${JOBS_DIR:-$DEFAULT_JOBS_DIR}"
STALE_THRESHOLD_SEC="${STALE_THRESHOLD_SEC:-7200}"  # 2 hours
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-300}"    # 5 minutes
LOG_FILE="${LOG_FILE:-$DEFAULT_LOG_FILE}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ONCE=0
INSTALL_PLIST=0
PLIST_TARGET=""
OUT_PLIST=""

# Parse arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --once)
            ONCE=1
            shift
            ;;
        --install-plist)
            INSTALL_PLIST=1
            if [ -n "$2" ] && [ "${2#--}" = "$2" ]; then
                PLIST_TARGET="$2"
                shift 2
            else
                PLIST_TARGET="$HOME/Library/LaunchAgents/com.videodubber.l0guard.plist"
                shift
            fi
            ;;
        --generate-plist)
            if [ -n "$2" ] && [ "${2#--}" = "$2" ]; then
                OUT_PLIST="$2"
                shift 2
            else
                OUT_PLIST="-"
                shift
            fi
            ;;
        --heartbeat-file)
            L1_HEARTBEAT_FILE="$2"
            shift 2
            ;;
        --jobs-dir)
            JOBS_DIR="$2"
            shift 2
            ;;
        --stale-threshold-sec)
            STALE_THRESHOLD_SEC="$2"
            shift 2
            ;;
        --interval-sec)
            CHECK_INTERVAL_SEC="$2"
            shift 2
            ;;
        --log-file)
            LOG_FILE="$2"
            shift 2
            ;;
        --python)
            PYTHON_BIN="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

render_plist() {
    target_out="$1"
    template_file="$SCRIPT_DIR/com.videodubber.l0guard.plist"
    mkdir -p "$PROJECT_ROOT/output/logs"

    if [ -f "$template_file" ]; then
        content=$(sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$template_file")
    else
        content="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>com.videodubber.l0guard</string>
    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>$PROJECT_ROOT/skills/video-dubber/scripts/l0_resident_guard.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$PROJECT_ROOT/output/logs/l0_launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_ROOT/output/logs/l0_launchd_err.log</string>
</dict>
</plist>"
    fi

    if [ -n "$target_out" ] && [ "$target_out" != "-" ]; then
        mkdir -p "$(dirname "$target_out")"
        printf "%s\n" "$content" > "$target_out"
        echo "Generated launchd plist at: $target_out"
    else
        printf "%s\n" "$content"
    fi
}

if [ "$INSTALL_PLIST" -eq 1 ]; then
    render_plist "$PLIST_TARGET"
    echo "L0 resident guard launchd plist installed to: $PLIST_TARGET"
    echo "To load the service, run:"
    echo "  launchctl unload \"$PLIST_TARGET\" 2>/dev/null || true"
    echo "  launchctl load \"$PLIST_TARGET\""
    exit 0
fi

if [ -n "$OUT_PLIST" ]; then
    render_plist "$OUT_PLIST"
    exit 0
fi

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$JOBS_DIR"
mkdir -p "$PROJECT_ROOT/output/logs"

log() {
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [L0_GUARD] $1" | tee -a "$LOG_FILE"
}

get_file_mtime() {
    file="$1"
    # macOS / BSD stat
    if stat -f %m "$file" 2>/dev/null; then
        return
    # Linux stat
    elif stat -c %Y "$file" 2>/dev/null; then
        return
    else
        "$PYTHON_BIN" -c "import os, sys; print(int(os.path.getmtime(sys.argv[1])))" "$file" 2>/dev/null || echo 0
    fi
}

check_l1_heartbeat() {
    NOW=$(date +%s)
    L1_HEALTHY=0
    if [ -f "$L1_HEARTBEAT_FILE" ]; then
        MTIME=$(get_file_mtime "$L1_HEARTBEAT_FILE")
        if [ -z "$MTIME" ] || [ "$MTIME" -le 0 ]; then
            MTIME=$NOW
        fi
        AGE=$((NOW - MTIME))
        if [ "$AGE" -le "$STALE_THRESHOLD_SEC" ]; then
            L1_HEALTHY=1
            log "OK: L1 heartbeat healthy (age: ${AGE}s <= threshold ${STALE_THRESHOLD_SEC}s)."
        else
            log "ALERT: L1 heartbeat stale (age: ${AGE}s > threshold ${STALE_THRESHOLD_SEC}s)."
        fi
    else
        log "WARNING: L1 heartbeat file not found ($L1_HEARTBEAT_FILE)."
    fi

    if [ "$L1_HEALTHY" -eq 0 ]; then
        EMERGENCY_HEARTBEAT_FILE="$(dirname "$L1_HEARTBEAT_FILE")/heartbeat_emergency.json"
        EMERGENCY_AGE=999999
        if [ -f "$EMERGENCY_HEARTBEAT_FILE" ]; then
            EM_MTIME=$(get_file_mtime "$EMERGENCY_HEARTBEAT_FILE")
            if [ -n "$EM_MTIME" ] && [ "$EM_MTIME" -gt 0 ]; then
                EMERGENCY_AGE=$((NOW - EM_MTIME))
            fi
        fi

        if [ "$EMERGENCY_AGE" -ge "$CHECK_INTERVAL_SEC" ]; then
            if [ ! -f "$L1_HEARTBEAT_FILE" ]; then
                log "Triggering initial sweep to scan jobs and start emergency patrol..."
            else
                log "Launching emergency patrol (L1 down, emergency patrol age: ${EMERGENCY_AGE}s)..."
            fi
            "$PYTHON_BIN" "$SCRIPT_DIR/cron_sweep_jobs.py" --emergency --jobs-dir "$JOBS_DIR" >> "$LOG_FILE" 2>&1 &
            log "Emergency patrol process dispatched in background."
        else
            log "L1 heartbeat remains stale; emergency patrol active or cooling down (age: ${EMERGENCY_AGE}s < ${CHECK_INTERVAL_SEC}s)."
        fi
    fi
}

log "L0 resident guard initialized. Heartbeat: $L1_HEARTBEAT_FILE, JobsDir: $JOBS_DIR, Threshold: ${STALE_THRESHOLD_SEC}s"

if [ "$ONCE" -eq 1 ]; then
    check_l1_heartbeat
    exit 0
fi

while true; do
    check_l1_heartbeat
    sleep "$CHECK_INTERVAL_SEC"
done
