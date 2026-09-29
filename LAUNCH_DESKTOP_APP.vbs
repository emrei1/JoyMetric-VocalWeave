' JoyMetric VocalWeave - silent entry point for the Desktop shortcut.
' Runs LAUNCH_DESKTOP_APP.ps1 fully hidden (no console flash), non-blocking.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = scriptDir & "\LAUNCH_DESKTOP_APP.ps1"
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
