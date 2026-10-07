@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" devUI.py --monitor
pause
