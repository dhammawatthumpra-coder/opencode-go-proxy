@echo off
REM OpenCode Go proxy - console mode (debug, Ctrl+C to stop)
REM Ignores config.json; edit the env block below if your port/upstream differ.
cd /d "%~dp0"
set PORT=8787
set PORT_SCAN=20
set UPSTREAM=https://opencode.ai/inference/go/v1
set SESSION_MODE=conversation
set LOG=1
set DUMP=1
set USAGE=1
node go-session-proxy.mjs
pause
