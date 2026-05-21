# Makes Zoro start automatically at Windows log-on — no admin required.
#
# Copies a silent launcher (zoro_autostart.vbs) into the current user's
# Startup folder. At log-on Windows runs it, which starts the supervisor
# with pythonw.exe (no console window); the supervisor brings up the agent,
# wake service, proactive loop, and web UI, and shows the tray icon.
#
# Run (in a normal PowerShell window):
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
# Undo with scripts\uninstall_autostart.ps1

$ErrorActionPreference = "Stop"

$src = Join-Path $PSScriptRoot "zoro_autostart.vbs"
if (-not (Test-Path $src)) { throw "launcher not found: $src" }

$startup = [Environment]::GetFolderPath("Startup")
$dest = Join-Path $startup "ZoroAssistant.vbs"

Copy-Item -Path $src -Destination $dest -Force

Write-Host "Installed Zoro auto-start." -ForegroundColor Green
Write-Host "Launcher: $dest"
Write-Host "Zoro will start automatically at your next log-on."
Write-Host "Start it now without logging out:  wscript `"$dest`""
