#!/bin/bash
# start-traderbot.sh — Launch the Python TraderBot with env vars loaded.
#
# Usage:
#   ./start-traderbot.sh              # Live trading (runs in background)
#   PAPER_TRADING=true ./start-traderbot.sh   # Paper trading / dry run
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check if already running
if pgrep -f "run_traderbot.py" > /dev/null 2>&1; then
  echo "[start-traderbot] Already running (PID: $(pgrep -f run_traderbot.py))"
  echo "[start-traderbot] Dashboard: http://localhost:3001"
  exit 0
fi

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Activate venv
source "$SCRIPT_DIR/.venv_py/bin/activate"

if [ "${PAPER_TRADING:-false}" = "true" ]; then
  echo "[start-traderbot] PAPER TRADING mode — no real transactions will be sent"
else
  echo "[start-traderbot] LIVE TRADING mode"
fi

echo "[start-traderbot] Starting bot in background..."
nohup python "$SCRIPT_DIR/packages/python/run_traderbot.py" >> "$SCRIPT_DIR/traderbot.out" 2>&1 &
BOT_PID=$!
echo $BOT_PID > /tmp/traderbot.pid
echo "[start-traderbot] Bot started, PID: $BOT_PID"
echo "[start-traderbot] Dashboard: http://localhost:3001"
echo "[start-traderbot] Logs:      tail -f $SCRIPT_DIR/traderbot.out"
