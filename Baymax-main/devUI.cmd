@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe devUI.py
if errorlevel 1 pause
