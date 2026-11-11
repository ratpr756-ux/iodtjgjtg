@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run install_windows.bat once first.
  pause
  exit /b 1
)
if not exist .venv\Scripts\pythonw.exe (
  echo Python windowless launcher was not found. Run install_windows.bat again.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0web_app.py"
exit /b 0
