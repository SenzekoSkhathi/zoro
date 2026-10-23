' Silent background launcher for Zoro Screen Relay
Dim root, pythonw, script, sh
root    = "C:\Users\senze\zoro"
pythonw = root & "\.venv\Scripts\pythonw.exe"
script  = root & "\src\zoro_screen_relay.py"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
' 0 = hidden window, False = don't wait for return
sh.Run """" & pythonw & """ """ & script & """", 0, False

