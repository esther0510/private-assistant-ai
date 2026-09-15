@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "APP_NAME=Private Assistant AI Alpha"
set "BOOTSTRAP_VERSION=2026-09-08.6"
set "PYTHON_SPEC=3.12"
set "UV=%~dp0tools\uv\uv.exe"
set "VENV=%~dp0.venv"
set "BOOTSTRAP_DIR=%~dp0.bootstrap"
set "REQ_FILE=%~dp0requirements.txt"
set "STATE_FILE=%BOOTSTRAP_DIR%\.bootstrap_state.json"
set "FINGERPRINT_FILE=%BOOTSTRAP_DIR%\fingerprint.sha256"
set "PYTHON_EXE=%VENV%\Scripts\python.exe"
set "PYTHONW_EXE=%VENV%\Scripts\pythonw.exe"
set "ENTRY=%~dp0run_silent.pyw"

if not defined UV_LINK_MODE set "UV_LINK_MODE=copy"

echo [%APP_NAME%] Preparing startup...

if not exist "%UV%" (
    echo.
    echo Missing bundled uv launcher:
    echo %UV%
    echo Please re-extract the test package, then run START.bat again.
    echo.
    pause
    exit /b 1
)

if not exist "%REQ_FILE%" (
    echo.
    echo Missing requirements.txt. Please re-extract the test package.
    echo.
    pause
    exit /b 1
)

if not exist "%ENTRY%" (
    echo.
    echo Missing app entry file: run_silent.pyw
    echo Please re-extract the test package.
    echo.
    pause
    exit /b 1
)

if not exist "%BOOTSTRAP_DIR%" mkdir "%BOOTSTRAP_DIR%" >nul 2>nul

set "BOOTSTRAP_VALUES="
for /f "usebackq tokens=1,2,3" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; function HashFile($path) { $sha=[Security.Cryptography.SHA256]::Create(); try { ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($path)))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() } }; $req=HashFile $env:REQ_FILE; $uv=HashFile $env:UV; $raw='bootstrap='+$env:BOOTSTRAP_VERSION+[Environment]::NewLine+'python='+$env:PYTHON_SPEC+[Environment]::NewLine+'requirements='+$req+[Environment]::NewLine+'uv='+$uv; $sha=[Security.Cryptography.SHA256]::Create(); try { $fp=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($raw)))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }; Write-Output ($req+' '+$uv+' '+$fp)"`) do (
    set "REQ_HASH=%%A"
    set "UV_HASH=%%B"
    set "FINGERPRINT=%%C"
)
if errorlevel 1 (
    echo.
    echo Could not calculate the bootstrap environment fingerprint.
    echo.
    pause
    exit /b 1
)
if not defined FINGERPRINT (
    echo.
    echo Could not calculate the bootstrap environment fingerprint.
    echo.
    pause
    exit /b 1
)

set "NEED_SYNC=0"
set "NEED_REBUILD=0"
set "SYNC_REASON="

if not exist "%VENV%\" (
    set "NEED_SYNC=1"
    set "SYNC_REASON=First run detected."
) else (
    if not exist "%PYTHON_EXE%" (
        set "NEED_SYNC=1"
        set "NEED_REBUILD=1"
        set "SYNC_REASON=The local Python environment is incomplete."
    )
    if not exist "%PYTHONW_EXE%" (
        set "NEED_SYNC=1"
        set "NEED_REBUILD=1"
        set "SYNC_REASON=The local Python environment is incomplete."
    )
)

set "OLD_FINGERPRINT="
if exist "%FINGERPRINT_FILE%" set /p OLD_FINGERPRINT=<"%FINGERPRINT_FILE%"
if not defined OLD_FINGERPRINT (
    set "NEED_SYNC=1"
    if not defined SYNC_REASON set "SYNC_REASON=Bootstrap state is missing."
)
if defined OLD_FINGERPRINT (
    if /I not "!OLD_FINGERPRINT!"=="!FINGERPRINT!" (
        set "NEED_SYNC=1"
        if exist "%VENV%\" set "NEED_REBUILD=1"
        set "SYNC_REASON=Requirements or Python environment settings changed."
    )
)
if "%NEED_SYNC%"=="1" (
    echo.
    echo !SYNC_REASON!
    echo This may take a few minutes and needs internet access.
    echo.

    if "%NEED_REBUILD%"=="1" (
        "%UV%" venv --clear --managed-python --python "%PYTHON_SPEC%" "%VENV%"
    ) else if exist "%VENV%\" (
        echo Reusing existing Python environment.
    ) else (
        "%UV%" venv --managed-python --python "%PYTHON_SPEC%" "%VENV%"
    )
    if errorlevel 1 (
        echo.
        echo Failed to create the private Python environment.
        echo Please check your internet connection and try START.bat again.
        echo.
        pause
        exit /b 1
    )

    set "RESOLVED_PYTHON_VERSION="
    for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; & $env:PYTHON_EXE -c 'import sys; print(sys.version.split()[0])'"`) do (
        set "RESOLVED_PYTHON_VERSION=%%V"
    )
    if errorlevel 1 (
        echo.
        echo Failed to verify the private Python environment.
        echo.
        pause
        exit /b 1
    )
    if not defined RESOLVED_PYTHON_VERSION (
        echo.
        echo Failed to verify the private Python environment.
        echo.
        pause
        exit /b 1
    )

    "%UV%" pip install --python "%PYTHON_EXE%" -r "%REQ_FILE%"
    if errorlevel 1 (
        echo.
        echo Failed to install app dependencies.
        echo Please check your internet connection and try START.bat again.
        echo.
        pause
        exit /b 1
    )

    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $state=[ordered]@{bootstrap_version=$env:BOOTSTRAP_VERSION; python_spec=$env:PYTHON_SPEC; resolved_python_version=$env:RESOLVED_PYTHON_VERSION; requirements_sha256=$env:REQ_HASH; uv_sha256=$env:UV_HASH; fingerprint=$env:FINGERPRINT; updated_at=(Get-Date).ToUniversalTime().ToString('o')}; $state | ConvertTo-Json | Set-Content -LiteralPath $env:STATE_FILE -Encoding UTF8; Set-Content -LiteralPath $env:FINGERPRINT_FILE -Value $env:FINGERPRINT -Encoding ASCII"
    if errorlevel 1 (
        echo.
        echo Failed to save bootstrap state.
        echo.
        pause
        exit /b 1
    )
) else (
    echo Environment is already ready.
)

if /I "%PAI_BOOTSTRAP_ONLY%"=="1" (
    echo Bootstrap check complete.
    exit /b 0
)

echo Starting Private Assistant AI...
start "" "%PYTHONW_EXE%" "%ENTRY%" --background
exit /b 0
