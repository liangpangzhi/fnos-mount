@echo off
cd /d "%~dp0"
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name FnosMount --icon "fnos_mount\assets\fnosmount.ico" --add-data "fnos_mount\assets;fnos_mount\assets" main.py
echo.
echo EXE output: dist\FnosMount.exe
pause
