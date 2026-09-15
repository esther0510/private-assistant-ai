Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(projectDir, "run_silent.pyw")
venvPythonw = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
shell.CurrentDirectory = projectDir
If fso.FileExists(venvPythonw) Then
    shell.Run """" & venvPythonw & """ """ & scriptPath & """", 0, False
Else
    shell.Run """" & scriptPath & """", 0, False
End If
