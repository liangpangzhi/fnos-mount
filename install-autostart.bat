@echo off
setlocal
cd /d "%~dp0"
set TASK=FnosMountAutoReconnect
if exist "%~dp0dist\FnosMount.exe" (
  set LAUNCH="%~dp0dist\FnosMount.exe" --tray --reconnect
) else (
  where pythonw >nul 2>&1
  if errorlevel 1 (
    echo pythonw not found. Install Python 3.8+ or build the EXE first.
    pause
    exit /b 1
  )
  for /f "delims=" %%i in ('where pythonw') do set PYW=%%i
  set LAUNCH="%PYW%" "%~dp0main.py" --tray --reconnect
)
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
schtasks /Create /TN "%TASK%" /TR "%LAUNCH%" /SC ONLOGON /RL HIGHEST /F
if errorlevel 1 (
  echo Failed to create scheduled task. Run this file as Administrator.
) else (
  echo Scheduled task installed: %TASK%
)
pause
