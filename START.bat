@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Bitte zuerst INSTALL.bat ausfuehren.
  pause
  exit /b 1
)
.venv\Scripts\python.exe app\main.py
if errorlevel 1 pause
