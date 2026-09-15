param([switch]$Clean)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Uv = Join-Path $Root 'tools\uv\uv.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Prepare the project .venv first.' }
& $Uv pip install --python $Python -r requirements.txt -r requirements-voice.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
$BuildArgs = @('-m', 'PyInstaller', 'private_assistant_ai_mvp.spec', '--noconfirm')
if ($Clean) { $BuildArgs += '--clean' }
# Avoid collecting unrelated DLLs from developer tools on PATH.
$BuildPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    & $Python @BuildArgs
} finally {
    $env:PATH = $BuildPath
}
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
Write-Host 'Built dist\PrivateAssistantAI\PrivateAssistantAI.exe (keep the full folder).'
