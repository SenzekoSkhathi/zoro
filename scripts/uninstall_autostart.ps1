# Removes Zoro's log-on auto-start (no admin required).
#
# Deletes the launcher from the Startup folder. Does not stop a supervisor
# that is already running — quit that from the tray icon.
#
# Run:
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1

$ErrorActionPreference = "Stop"

$startup = [Environment]::GetFolderPath("Startup")
$dest = Join-Path $startup "ZoroAssistant.vbs"

if (Test-Path $dest) {
    Remove-Item $dest -Force
    Write-Host "Removed Zoro auto-start ($dest)." -ForegroundColor Green
} else {
    Write-Host "Zoro auto-start was not installed — nothing to do."
}
Write-Host "If Zoro is still running, quit it from the tray icon."
