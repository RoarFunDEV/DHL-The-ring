@echo off
REM Windows: double-click to open the booth control panel.
cd /d "%~dp0"
start "" pythonw booth_control.py
if errorlevel 1 python booth_control.py
