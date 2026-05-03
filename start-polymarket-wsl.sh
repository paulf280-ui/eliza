#!/bin/bash
# PF Capital — Polymarket Bot launcher (called from start-polymarket.bat)
cd /home/paulf/eliza

# Start the Next.js dashboard (port 3002) if not already running
if ! lsof -i:3002 -t &>/dev/null; then
    echo "[*] Starting Polymarket dashboard on port 3002..."
    cd packages/polymarket-dashboard
    nohup npm start >> /home/paulf/eliza/polymarket-dashboard.out 2>&1 &
    cd /home/paulf/eliza
    echo "[+] Dashboard starting..."
fi

# Start the Python trading bot if not already running
if ! pgrep -f run_polymarket.py &>/dev/null; then
    echo "[*] Starting Polymarket trading bot..."
    nohup .venv_py/bin/python packages/python/run_polymarket.py >> /home/paulf/eliza/polymarket.out 2>&1 &
    echo "[+] Bot starting (PID: $!)"
else
    echo "[+] Bot already running (PID: $(pgrep -f run_polymarket.py))"
fi

echo "[+] All systems launching..."
