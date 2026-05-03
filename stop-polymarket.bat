@echo off
title PF Capital - Stop Polymarket Bot
color 0C

echo.
echo  [*] Stopping Polymarket Bot...
wsl -d Ubuntu -e bash -c "pkill -f run_polymarket.py 2>/dev/null; fuser -k 3002/tcp 2>/dev/null; fuser -k 3003/tcp 2>/dev/null; echo done"
echo.
echo  [+] Polymarket Bot stopped.
echo  [+] Telegram remote control also offline.
echo.
timeout /t 2 /nobreak > nul
