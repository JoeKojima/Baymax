@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\bootstrap.ps1" -Action monitor
if errorlevel 1 pause
