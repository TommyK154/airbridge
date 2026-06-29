@echo off
REM AirBridge launcher for Windows.
REM Requires uv on PATH:  https://docs.astral.sh/uv/
REM Pass extra options through, e.g.  run.bat --port 9000

cd /d "%~dp0"
uv run main.py %*

echo.
echo AirBridge has stopped. Press any key to close this window.
pause >nul
