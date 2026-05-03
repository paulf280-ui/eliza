@echo off
title PF Capital - Stop TraderBot
color 0C

echo.
echo  [*] Stopping TraderBot...
wsl -d Ubuntu -e bash -c "pkill -f run_traderbot.py 2>/dev/null; fuser -k 3001/tcp 2>/dev/null; echo done"
echo.
echo  [+] TraderBot stopped.
echo.
timeout /t 2 /nobreak > nul
