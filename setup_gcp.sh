#!/usr/bin/env bash
# =============================================================================
# OANDA Flip Bot - one-shot setup for a Google Cloud Compute Engine VM (Ubuntu).
# Run this on a fresh VM, then load your API token into .env and start.
#
#   bash setup_gcp.sh
# =============================================================================
set -e

echo "==> Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3-pip screen

echo "==> Installing Python dependencies..."
pip3 install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!! Created .env from .env.example"
  echo "!! EDIT IT NOW:  nano .env   and paste OANDA_API_TOKEN + OANDA_ACCOUNT_ID"
  echo "!! Do NOT commit .env (it's in .gitignore)."
  echo "!! Convert .env -> env vars so python-dotenv is optional:"
  echo "!!   set -a; source .env; set +a"
fi

# --- Install a systemd service so the bot auto-starts and survives reboots ---
echo "==> Installing systemd service 'flip-bot' ..."
sudo tee /etc/systemd/system/flip-bot.service >/dev/null <<'SVC'
[Unit]
Description=OANDA Flip Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/<USER>/bot
EnvironmentFile=/home/<USER>/bot/.env
ExecStart=/usr/bin/python3 /home/<USER>/bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC
# Replace the <USER> placeholder with the actual user.
sudo sed -i "s|<USER>|$(whoami)|g" /etc/systemd/system/flip-bot.service
sudo systemctl daemon-reload
echo "==> Enabling service (will start on boot)."
sudo systemctl enable flip-bot

echo ""
echo "============================================================================"
echo " DONE. Next steps:"
echo "   1) nano .env           # paste your OANDA_API_TOKEN and OANDA_ACCOUNT_ID"
echo "   2) source .env         # load vars (or edit the service's EnvironmentFile)"
echo "   3) sudo systemctl start flip-bot     # start the bot"
echo "   4) journalctl -u flip-bot -f         # watch the bot log"
echo "   For the dashboard:  python3 app.py  (opens on port 5000)"
echo "       -> open tcp:5000 in the firewall, then visit http://<VM-IP>:5000"
echo "============================================================================"
