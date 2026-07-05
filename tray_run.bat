@echo off
REM AirBridge tray launcher for Windows.
REM Requires uv on PATH:  https://docs.astral.sh/uv/
REM Pass server options through, e.g.  tray_run.bat --port 9000
REM The Windows login registry entry created by the tray toggles points here.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

REM Provision the venv with every extra: the tray needs pystray and Pillow,
REM and syncing all extras keeps uv from pruning packages (thumbnails, tls)
REM installed by other launch commands.
uv sync --all-extras --quiet
if errorlevel 1 (
    echo uv sync failed. Is uv installed and on PATH?
    pause
    exit /b 1
)

REM tray.py detaches itself from this console (hidden respawn) and the first
REM process exits right away, so this window closes on its own and closing
REM any terminal cannot kill the tray. Output goes to .airbridge\tray.log.
".venv\Scripts\python.exe" "%~dp0tray.py" %*
