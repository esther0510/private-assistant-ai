@echo off
setlocal

cd /d "%~dp0"

echo This will rebuild only the app's local Python environment.
echo It will NOT delete reminders, learning data, settings, or other user data in AppData.
echo.

if exist "%~dp0.venv" (
    rmdir /s /q "%~dp0.venv"
)

if exist "%~dp0.bootstrap" (
    rmdir /s /q "%~dp0.bootstrap"
)

echo Local environment reset complete.
echo Run START.bat to rebuild and launch the assistant.
echo.
if /I "%PAI_RESET_NO_PAUSE%"=="1" exit /b 0
pause
