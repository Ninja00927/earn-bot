#!/bin/bash
# Google Cloud VM startup script for the earn bot. Runs as root on every boot:
# installs (first boot) or updates (git pull) the bot, writes .env from instance
# metadata (bot-token, admin-ids) and (re)starts it as a systemd service.
set -euo pipefail

BRANCH=claude/elegant-pasteur-7ghqyy
REPO=https://github.com/fluixmc02-sketch/MUSS-Student-Union.git
DIR=/opt/bot
APP=$DIR/earn_bot
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes

meta() { curl -fs -H "Metadata-Flavor: Google" "$MD/$1"; }

if [ ! -d "$DIR/.git" ]; then
    apt-get update
    apt-get install -y git python3-venv
    git clone -b "$BRANCH" "$REPO" "$DIR"
else
    git -C "$DIR" pull --ff-only || true
fi

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# The database lives outside the git checkout so updates never touch it.
mkdir -p /var/lib/earnbot
umask 077
cat > "$APP/.env" <<EOF
BOT_TOKEN=$(meta bot-token)
ADMIN_IDS=$(meta admin-ids)
DB_PATH=/var/lib/earnbot/earn.db
EOF

cat > /etc/systemd/system/earnbot.service <<EOF
[Unit]
Description=Earn Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable earnbot
systemctl restart earnbot
