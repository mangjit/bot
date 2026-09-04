#!/usr/bin/env bash
# =============================================================================
# OANDA Flip Bot - ONE-command launcher (with a bot supervisor)
#
# Starts everything from a single command so all processes share the same
# container/filesystem (state.json + control.json are shared, so BOTH the
# dashboard and Telegram can fully control the live bot).
#
#   1) The TRADING BOT (bot.py) is managed by a supervisor loop that honors
#      the "stop" flag in control.json. dashboard/Telegram Start/Stop buttons
#      only write that flag:
#          stop=false  -> supervisor keeps bot.py running
#          stop=true   -> bot.py reads it, closes positions, exits; supervisor
#                         holds it stopped until you press Start
#   2) telegram_bot.py  - Telegram control daemon (always runs)
#   3) app.py           - the web dashboard (Flask), served via gunicorn
#
# Usage (local):   bash start.sh
# Usage (Render):  startCommand: bash start.sh
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

ensure_control() {
  # create control.json if it doesn't exist (default: not stopped)
  if [ ! -f control.json ]; then
    echo '{"stop": false}' > control.json
  fi
}

get_stop_flag() {
  python3 -c "import json,os;print(open('control.json').read().get(stop))" 2>/dev/null || \
  python3 -c "import json;print(json.load(open('control.json')).get('stop',False))" 2>/dev/null || echo "False"
}

# --- Trading bot supervisor ----------------------------------------------
# Keeps bot.py running unless control.json has "stop": true.
echo "[start] control.json supervisor starting for bot.py ..."
ensure_control
(
  while true; do
    STOP="$(python3 -c "import json;print(json.load(open('control.json')).get('stop',False))" 2>/dev/null || echo "False")"
    if [ "$STOP" = "True" ]; then
      # stopped by dashboard/Telegram: hold off until Start is pressed.
      echo "[start] bot held STOPPED (control.json stop=true). Press Start to resume."
      sleep 5
    else
      python bot.py >> flip_bot.log 2>&1
      echo "[start] bot.py exited (code $?); will restart in 5s unless stopped."
      sleep 5
    fi
  done
) &
SUPER_PID=$!

echo "[start] launching telegram daemon (telegram_bot.py) ..."
python telegram_bot.py >> flip_bot.log 2>&1 &
TG_PID=$!
echo "[start] telegram pid=$TG_PID (feeding flip_bot.log)"

trap 'kill $SUPER_PID $TG_PID 2>/dev/null' EXIT

echo "[start] launching dashboard (gunicorn app:app) ..."
PORT="${PORT:-5000}"
exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
