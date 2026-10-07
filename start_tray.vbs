' Launches the tray app with no console window, fully detached from the terminal.
' WScript.Shell.Run with window style 0 and bWaitOnReturn False means the process
' is not tied to the console that ran this script, so closing it changes nothing.
Option Explicit

Dim shell, fso, base, pyw, script

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = base & "\.venv\Scripts\pythonw.exe"
script = base & "\proxy_tray.py"

If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"   ' fall back to whatever is on PATH
End If

If Not fso.FileExists(script) Then
    WScript.Echo "proxy_tray.py not found next to this script."
    WScript.Quit 1
End If

shell.CurrentDirectory = base
shell.Run """" & pyw & """ """ & script & """", 0, False
