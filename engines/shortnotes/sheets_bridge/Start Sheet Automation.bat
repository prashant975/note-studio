@echo off
REM Double-click launcher: starts the one-button Sheet Automation page.
cd /d "%~dp0.."
python -m sheets_bridge --serve
pause
