@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\bootstrap.ps1" -Action stop
if errorlevel 1 pause
