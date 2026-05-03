#!/bin/bash
# watchdog.sh — Supervisor that keeps the TraderBot alive.
#
# The bot process can self-restart by sending itself SIGTERM; this script
# catches the exit and brings a fresh process up within 5 seconds.
# If the bot crashes 5+ times inside 60 seconds, the watchdog stops to
# prevent thrashing on a broken startup (e.g. import error, bad config).
#
# Logs are rotated: on watchdog startup, and whenever the current log
# exceeds LOG_ROTATE_MB. Archived logs are kept in ./logs/ indefinitely
# so Jarvis can always look back on history.
#
# Usage (from start-bot-wsl.sh):
#   setsid nohup bash watchdog.sh < /dev/null >> /dev/null 2>&1 &
#   echo $! > /tmp/traderbot-watchdog.pid

set -uo pipefail
cd /home/paulf/eliza

PIDFILE=/tmp/traderbot-watchdog.pid
BOT_PIDFILE=/tmp/traderbot-bot.pid
MAX_CRASHES=5
CRASH_WINDOW=60   # seconds — crash rate window
RESTART_DELAY=5   # seconds between restarts
LOG=traderbot.out
LOG_DIR=logs
LOG_ROTATE_MB=20  # rotate when log exceeds this size

# Write our own PID so start scripts can find and kill us
echo $$ > "$PIDFILE"

_ts() { date '+%Y-%m-%d %H:%M:%S UTC'; }

# Rotate the current log into logs/ with a timestamp suffix.
# Safe to call while the bot is not running (between restarts).
rotate_log() {
    if [ ! -f "$LOG" ]; then
        return
    fi
    local size_bytes
    size_bytes=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if (( size_bytes < 1024 )); then
        return  # nothing worth archiving
    fi
    mkdir -p "$LOG_DIR"
    local archive="$LOG_DIR/traderbot-$(date '+%Y%m%d_%H%M%S').out"
    mv "$LOG" "$archive"
    echo "[watchdog] $(_ts) Log rotated → $archive ($(( size_bytes / 1024 / 1024 ))MB archived)" >> "$LOG"
}

# Check if log needs rotation (exceeds size threshold)
needs_rotation() {
    if [ ! -f "$LOG" ]; then
        return 1
    fi
    local size_bytes
    size_bytes=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    local threshold=$(( LOG_ROTATE_MB * 1024 * 1024 ))
    (( size_bytes >= threshold ))
}

cleanup() {
    echo "[watchdog] $(_ts) Watchdog received shutdown signal — stopping." >> "$LOG"
    # Kill the bot if it's still running
    if [ -f "$BOT_PIDFILE" ]; then
        BOT_PID=$(cat "$BOT_PIDFILE" 2>/dev/null)
        kill "$BOT_PID" 2>/dev/null
    fi
    rm -f "$PIDFILE" "$BOT_PIDFILE"
    exit 0
}
trap cleanup SIGTERM SIGINT

crash_times=()

# Always rotate on startup — each watchdog session gets a fresh log
rotate_log

echo "[watchdog] $(_ts) Watchdog started (PID=$$, crash limit=${MAX_CRASHES}/${CRASH_WINDOW}s)" >> "$LOG"

while true; do
    # Rotate if log has grown too large (between bot restarts, safe window)
    if needs_rotation; then
        rotate_log
        echo "[watchdog] $(_ts) Auto-rotated oversized log (>${LOG_ROTATE_MB}MB)" >> "$LOG"
    fi

    echo "[watchdog] $(_ts) Launching bot..." >> "$LOG"

    # Start bot in foreground so watchdog captures its exit code
    .venv_py/bin/python packages/python/run_traderbot.py >> "$LOG" 2>&1 &
    BOT_PID=$!
    echo $BOT_PID > "$BOT_PIDFILE"

    # Wait for bot to exit
    wait $BOT_PID 2>/dev/null
    EXIT_CODE=$?
    rm -f "$BOT_PIDFILE"

    NOW=$(date +%s)
    crash_times+=("$NOW")

    # Evict timestamps older than CRASH_WINDOW
    new_times=()
    for t in "${crash_times[@]}"; do
        if (( NOW - t < CRASH_WINDOW )); then
            new_times+=("$t")
        fi
    done
    crash_times=("${new_times[@]}")

    if (( ${#crash_times[@]} >= MAX_CRASHES )); then
        echo "[watchdog] $(_ts) CRASH LOOP DETECTED: ${#crash_times[@]} exits in ${CRASH_WINDOW}s — watchdog stopping to prevent thrashing. Fix the error and restart manually." >> "$LOG"
        rm -f "$PIDFILE"
        exit 1
    fi

    echo "[watchdog] $(_ts) Bot exited (code=$EXIT_CODE) — restarting in ${RESTART_DELAY}s..." >> "$LOG"
    sleep "$RESTART_DELAY"
done
