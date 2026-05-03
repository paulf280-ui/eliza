#!/bin/bash
# Called by the Windows .bat launcher — starts the watchdog which supervises the bot.
# The watchdog auto-restarts the bot on crash or SIGTERM (Jarvis restart command).
# Uses setsid so the watchdog + bot survive after this shell exits.
cd /home/paulf/eliza

# Kill any existing watchdog first (prevents double-watchdog on repeated clicks)
if [ -f /tmp/traderbot-watchdog.pid ]; then
    OLD_WD=$(cat /tmp/traderbot-watchdog.pid 2>/dev/null)
    if [ -n "$OLD_WD" ] && kill -0 "$OLD_WD" 2>/dev/null; then
        echo "[launcher] Stopping existing watchdog (PID=$OLD_WD)..."
        kill "$OLD_WD" 2>/dev/null
        sleep 2
    fi
    rm -f /tmp/traderbot-watchdog.pid
fi

# Kill anything still on port 3001
fuser -k 3001/tcp 2>/dev/null
sleep 1

# Launch watchdog in a fully detached new session
# The watchdog then launches the bot as its child
setsid nohup bash /home/paulf/eliza/watchdog.sh < /dev/null > /dev/null 2>&1 &

echo "started watchdog pid=$!"
