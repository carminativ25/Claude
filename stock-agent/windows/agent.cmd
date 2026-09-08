@echo off
rem Runs the agent with the keys from .env, e.g.:  windows\agent.cmd check
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Virtual environment not found. Run windows\setup.cmd first.
  exit /b 1
)
if exist .env (
  for /f "usebackq tokens=1,* delims==" %%a in (`findstr /r "^[A-Za-z_]" .env`) do set "%%a=%%b"
)
.venv\Scripts\python.exe -m stock_agent %*
endlocal
