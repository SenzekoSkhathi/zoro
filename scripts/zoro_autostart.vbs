' Silent launcher for the Zoro supervisor.
' Runs pythonw (no console window) so Zoro starts invisibly at log-on.
' A copy of this file is placed in the user's Startup folder by
' install_autostart.ps1; delete it (or run uninstall_autostart.ps1) to stop
' Zoro starting automatically.

Dim root, pythonw, script, sh
root    = "C:\Users\senze\zoro"
pythonw = root & "\.venv\Scripts\pythonw.exe"
script  = root & "\src\zoro_supervisor.py"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
' window style 0 = hidden, False = don't wait
sh.Run """" & pythonw & """ """ & script & """", 0, False
