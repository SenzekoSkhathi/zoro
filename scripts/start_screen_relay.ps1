# Start Zoro Screen Relay in the current Windows logged-in session
$root = "C:\Users\senze\zoro"
$pythonw = "$root\.venv\Scripts\pythonw.exe"
$script = "$root\src\zoro_screen_relay.py"

Write-Host "Starting Zoro Screen Relay in background..." -ForegroundColor Cyan
Start-Process -FilePath $pythonw -ArgumentList $script -WorkingDirectory $root -WindowStyle Hidden
Write-Host "Zoro Screen Relay started! Connected to Azure Redis." -ForegroundColor Green
