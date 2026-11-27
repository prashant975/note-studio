@echo off
rem Starts NoteCraft and opens it in your browser. Keep this window open while you work.
cd /d "%~dp0"
title NoteCraft
if not exist ".venv\Scripts\python.exe" (
    py -3.13 scripts\setup.py
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" run_studio.py
if errorlevel 1 pause
