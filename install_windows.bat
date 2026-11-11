@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher was not found. Install Python 3.11 or newer first.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 (
  echo Python 3.11 or newer was not found. Install it and try again.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist .env copy .env.example .env >nul
echo.
echo Setup is complete. Install FFmpeg and FFprobe, then double-click "Start ClipFlow.bat".
pause
