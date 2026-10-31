@echo off
rem Starts NoteCraft and opens it in your browser. Keep this window open while you work.
cd /d "%~dp0"
title NoteCraft
python run_studio.py
if errorlevel 1 pause
