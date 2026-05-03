# Creates the TraderBot desktop shortcut with custom icon
# Run once from PowerShell: powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\paulf\eliza\create-shortcut.ps1"

$WshShell   = New-Object -ComObject WScript.Shell
$Desktop    = [System.Environment]::GetFolderPath("Desktop")
# Also check OneDrive Desktop
if (-not (Test-Path "$Desktop\dummy_test_check")) {
    $OneDriveDesktop = "$env:USERPROFILE\OneDrive\Desktop"
    if (Test-Path $OneDriveDesktop) { $Desktop = $OneDriveDesktop }
}

$BatPath    = "\\wsl.localhost\Ubuntu\home\paulf\eliza\start-traderbot.bat"
$IconPath   = "$env:USERPROFILE\traderbot.ico"

# ── START shortcut ────────────────────────────────────────────────────────────
$Shortcut = $WshShell.CreateShortcut("$Desktop\TraderBot.lnk")
$Shortcut.TargetPath       = $BatPath
$Shortcut.WorkingDirectory = "\\wsl.localhost\Ubuntu\home\paulf\eliza"
$Shortcut.Description      = "PF Capital - Autonomous Solana Trading Bot"
$Shortcut.IconLocation     = "$IconPath,0"
$Shortcut.WindowStyle      = 1
$Shortcut.Save()
Write-Host "START shortcut: $Desktop\TraderBot.lnk" -ForegroundColor Green

# ── STOP shortcut ─────────────────────────────────────────────────────────────
$StopBat = "\\wsl.localhost\Ubuntu\home\paulf\eliza\stop-traderbot.bat"
$StopShortcut = $WshShell.CreateShortcut("$Desktop\Stop TraderBot.lnk")
$StopShortcut.TargetPath       = $StopBat
$StopShortcut.WorkingDirectory = "\\wsl.localhost\Ubuntu\home\paulf\eliza"
$StopShortcut.Description      = "Stop the TraderBot process"
$StopShortcut.IconLocation     = "$IconPath,0"
$StopShortcut.WindowStyle      = 1
$StopShortcut.Save()
Write-Host "STOP shortcut:  $Desktop\Stop TraderBot.lnk" -ForegroundColor Red

Write-Host ""
Write-Host "Done! Two shortcuts on your desktop:" -ForegroundColor Cyan
Write-Host "  TraderBot         — double-click to start bot + open dashboard" -ForegroundColor Cyan
Write-Host "  Stop TraderBot    — double-click to stop the bot" -ForegroundColor Cyan
