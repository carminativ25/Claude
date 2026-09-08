@echo off
rem One-time setup on Windows: creates a virtual environment, installs dependencies, creates .env
setlocal
cd /d "%~dp0.."

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH" in the installer.
  echo Then run this script again.
  exit /b 1
)

echo Using: %PY%
%PY% -m venv .venv || exit /b 1
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt || exit /b 1
if not exist .env copy .env.example .env >nul

echo.
echo Setup complete.
echo 1. Open .env in Notepad and paste your Alpaca paper keys (and an Anthropic key if you have one):
echo       notepad .env
echo 2. Then run:  windows\agent.cmd check
endlocal
