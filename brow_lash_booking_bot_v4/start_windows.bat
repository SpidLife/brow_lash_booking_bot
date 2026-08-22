@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Создан файл .env. Открой его, вставь BOT_TOKEN и ADMIN_IDS, затем запусти этот файл снова.
  pause
  exit /b 1
)
python run.py
pause
