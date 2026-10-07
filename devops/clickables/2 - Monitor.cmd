@echo off
setlocal
set "EMBER_PYTHON=%~dp0..\..\Baymax-main\.venv\Scripts\python.exe"
if not exist "%EMBER_PYTHON%" set "EMBER_PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%EMBER_PYTHON%" (
  echo Install the environment using the README before opening Ember.
  pause
  exit /b 1
)
"%EMBER_PYTHON%" "%~dp0launch.py" monitor
if errorlevel 1 pause
