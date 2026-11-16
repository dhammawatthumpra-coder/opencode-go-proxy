@echo off
REM OpenCode Go proxy - tray mode (no console window)
cd /d "%~dp0"
start "" /min pythonw.exe tray_app.py
