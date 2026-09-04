#!/usr/bin/env bash
# =============================================================================
# OANDA Flip Bot - ONE-command launcher
#
# Starts everything from a single command so all processes share the same
# container/filesystem (state.json + control.json are shared, so Telegram can
# fully control the live bot).
#
#   1) bot.py            - the trading loop
#   2) telegram_bot.py   - Telegram control daemon
#   3) app.py            - the web dashboard (Flask), served via gunicorn
#
# Usage (local):   bash start.sh
# Usage (Render):  startCommand: bash start.sh
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
echo "[start] launching trader (bot.py) ..."
python bot.py >> flip_bot.log 2>&1 &
BOT_PID=$!
echo "[start] trader pid=$BOT_PID (feeding flip_bot.log)"

echo "[start] launching telegram daemon (telegram_bot.py) ..."
python telegram_bot.py >> flip_bot.log 2>&1 &
TG_PID=$!
echo "[start] telegram pid=$TG_PID (feeding flip_bot.log)"

# keep the whole thing alive if a child dies (Render restarts the web process)
trap 'kill $BOT_PID $TG_PID 2>/dev/null' EXIT

echo "[start] launching dashboard (gunicorn app:app) ..."
# App must bind to $PORT (Render sets it; default 5000 locally).
PORT="${PORT:-5000}"
exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
