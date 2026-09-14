@echo off
cd /d "%~dp0"
echo Installing the booth components...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Done. Double-click "RoarFun Booth.bat" to start.
pause
