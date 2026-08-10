@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo FEHLER: Python Launcher py wurde nicht gefunden.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3.13 -m venv .venv 2>nul
  if errorlevel 1 py -3.12 -m venv .venv 2>nul
  if errorlevel 1 py -3.11 -m venv .venv
  if errorlevel 1 (
    echo FEHLER: Keine geeignete Python-Version gefunden.
    pause
    exit /b 1
  )
)
set PIP_ACTION=install
.venv\Scripts\python.exe -m pip %PIP_ACTION% -r requirements.txt
if errorlevel 1 (
  echo FEHLER: Abhaengigkeiten konnten nicht installiert werden.
  pause
  exit /b 1
)
echo Installation abgeschlossen.
pause
