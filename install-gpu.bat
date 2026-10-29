@echo off
rem Optional: adds GPU Whisper (faster-whisper + CUDA libraries, ~1.5 GB) for NVIDIA cards.
rem Not needed for the default CPU models. Run run.bat afterwards.
setlocal
cd /d "%~dp0"
where uv >nul 2>nul || set "PATH=%USERPROFILE%\.local\bin;%PATH%"
uv sync --no-dev --inexact --extra gpu
pause
