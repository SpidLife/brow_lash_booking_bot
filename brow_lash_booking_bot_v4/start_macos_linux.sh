#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env. Вставь в него BOT_TOKEN и ADMIN_IDS, затем запусти скрипт снова."
  exit 1
fi
python3 run.py
