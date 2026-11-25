@echo off
schtasks /Delete /TN "FnosMountAutoReconnect" /F
if errorlevel 1 (
  echo Task not found or deletion failed.
) else (
  echo Scheduled task removed.
)
pause
