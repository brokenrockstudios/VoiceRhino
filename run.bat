@echo off
rem VoiceRhino launcher. First run installs uv + Python + packages (a few minutes),
rem later runs just start the app. The CC icon appears in the system tray.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    ) else (
        echo Installing uv, the Python package manager from astral.sh ...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    )
)

rem --inexact keeps optional extras (GPU Whisper) if install-gpu.bat added them.
uv sync --no-dev --inexact
if errorlevel 1 (
    echo.
    echo Setup failed - see the messages above.
    pause
    exit /b 1
)

start "" ".venv\Scripts\voicerhino-gui.exe" %*
