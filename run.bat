@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.8+ and add it to PATH.
  pause
  exit /b 1
)
python main.py
pause
