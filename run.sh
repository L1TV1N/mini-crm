#!/usr/bin/env bash
# Локальный запуск: переменные берутся из .env (он не коммитится), токен не попадает ни в историю команд, ни в git.
cd "$(dirname "$0")" || exit 1
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec python3 server.py
