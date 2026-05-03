#!/bin/bash
# start_bot.sh — starts traderbot with auto log rotation
LOG="packages/dashboard/traderbot.out"
MAX_LINES=3000

cd /home/paulf/eliza

# Trim log if it's already large
if [ -f "$LOG" ]; then
  lines=$(wc -l < "$LOG")
  if [ "$lines" -gt "$MAX_LINES" ]; then
    tail -$MAX_LINES "$LOG" > /tmp/tb_trim.txt && mv /tmp/tb_trim.txt "$LOG"
    echo "[start_bot] Log trimmed from $lines to $MAX_LINES lines"
  fi
fi

# Start bot in background
nohup .venv_py/bin/python packages/python/run_traderbot.py >> "$LOG" 2>&1 &
BOT_PID=$!
echo "[start_bot] Bot started — PID $BOT_PID"

# Background watchdog: trim log every 5 minutes
(
  while kill -0 $BOT_PID 2>/dev/null; do
    sleep 300
    lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    if [ "$lines" -gt "$MAX_LINES" ]; then
      tail -$MAX_LINES "$LOG" > /tmp/tb_trim.txt && mv /tmp/tb_trim.txt "$LOG"
    fi
  done
) &

wait $BOT_PID
echo "[start_bot] Bot exited"
