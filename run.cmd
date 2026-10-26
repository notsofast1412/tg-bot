@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run setup.ps1 first. See README.md.
  pause
  exit /b 1
)
start "PaintWatch" ".venv\Scripts\pythonw.exe" "run_app.py" %*
