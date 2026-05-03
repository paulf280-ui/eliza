@echo off
chcp 65001 > nul
title PF Capital - Polymarket Bot
color 0B

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set DASHBOARD=http://localhost:3002/

echo.
echo  ================================================
echo   PF CAPITAL  -  Polymarket Intelligence Engine
echo  ================================================
echo.

:: Check if bot is already running (port 3002 listening)
netstat -an 2>nul | find "3002" | find "LISTENING" > nul
if not errorlevel 1 (
    echo  [+] Polymarket Bot is already running!
    echo  [+] Opening dashboard...
    start "" %CHROME% %DASHBOARD%
    echo.
    echo  Dashboard: %DASHBOARD%
    echo  Press any key to close this window...
    pause > nul
    exit /b
)

:: Not running - start it via WSL
echo  [*] Starting Polymarket Bot...
wsl -d Ubuntu bash /home/paulf/eliza/start-polymarket-wsl.sh

echo  [*] Waiting for bot and dashboard to start...
echo.

set /a waited=0
:wait_loop
timeout /t 3 /nobreak > nul
set /a waited+=3
netstat -an 2>nul | find "3002" | find "LISTENING" > nul
if not errorlevel 1 goto ready
echo  [.] %waited%s elapsed...
if %waited% geq 60 goto open_anyway
goto wait_loop

:ready
echo.
echo  [+] Polymarket Bot is LIVE  (%waited%s)
echo  [+] Opening dashboard...
echo.
start "" %CHROME% %DASHBOARD%
echo  ================================================
echo   Dashboard:   %DASHBOARD%
echo   Telegram:    @fitzerspolymarketbot
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
