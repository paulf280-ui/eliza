@echo off
chcp 65001 > nul
title PF Capital - TraderBot
color 0A

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set DASHBOARD=http://localhost:3001/

echo.
echo  ================================================
echo   PF CAPITAL  -  Autonomous Solana Signal Engine
echo  ================================================
echo.

:: Check if bot is already running (port 3001 listening)
netstat -an 2>nul | find "3001" | find "LISTENING" > nul
if not errorlevel 1 (
    echo  [+] Bot is already running!
    echo  [+] Opening dashboard...
    start "" %CHROME% %DASHBOARD%
    echo.
    echo  Dashboard: %DASHBOARD%
    echo  Press any key to close this window...
    pause > nul
    exit /b
)

:: Not running - start it via WSL
echo  [*] Starting TraderBot...
wsl -d Ubuntu bash /home/paulf/eliza/start-bot-wsl.sh

echo  [*] Waiting for bot to start...
echo.

set /a waited=0
:wait_loop
timeout /t 3 /nobreak > nul
set /a waited+=3
netstat -an 2>nul | find "3001" | find "LISTENING" > nul
if not errorlevel 1 goto ready
echo  [.] %waited%s elapsed...
if %waited% geq 45 goto open_anyway
goto wait_loop

:ready
echo.
echo  [+] TraderBot is LIVE  (%waited%s)
echo  [+] Opening dashboard...
echo.
start "" %CHROME% %DASHBOARD%
echo  ================================================
echo   Dashboard: %DASHBOARD%
echo   Bot running in background - minimise this window
echo  ================================================
echo.
pause > nul
exit /b

:open_anyway
echo.
echo  [*] Opening now - refresh if page is empty...
start "" %CHROME% %DASHBOARD%
pause > nul
