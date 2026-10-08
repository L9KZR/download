@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist .env (
  copy .env.example .env >nul
  notepad .env
  echo Save BOT_TOKEN and OWNER_ID in .env then run again.
  pause
  exit /b
)
.venv\Scripts\python.exe bot.py
:fail
pause
