@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title UPSC_Notes

echo ================================================
echo    UPSC_Notes
echo ================================================
echo.

REM --- 1. Find Python -------------------------------------------------------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ERROR] Python was not found on this computer.
  echo.
  echo   1. Install Python 3.10 or newer from:
  echo        https://www.python.org/downloads/
  echo   2. On the first install screen, TICK "Add Python to PATH".
  echo   3. Then run this file again.
  echo.
  pause
  exit /b 1
)

REM --- 2. Create the environment (first run only) --------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. This runs only once...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create the Python environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

REM --- 3. Install dependencies (first run only) ----------------------------
if not exist ".venv\.deps_installed" (
  echo Installing components. This can take a few minutes the first time...
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Could not install the required components. Check your internet
    echo         connection and run this file again.
    pause
    exit /b 1
  )
  echo done> ".venv\.deps_installed"
)

REM --- 4. Launch -----------------------------------------------------------
echo.
echo Opening UPSC_Notes in your browser at http://localhost:8501
echo Sign in with your @pw.live Google account.
echo.
echo KEEP THIS WINDOW OPEN while you use the app. Close it to stop.
echo.
REM Port 8501 is required: the Google sign-in is registered for that exact port.
python -m streamlit run app.py --server.port 8501
pause
