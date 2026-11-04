#!/usr/bin/env bash
# Install the bot on a fresh server. Idempotent: safe to re-run.
#
#   sudo bash deploy/install.sh            # venv in .venv, systemd unit, enable+start
#   sudo bash deploy/install.sh --no-start # install only
#
# Assumes Debian/Ubuntu with systemd and python3 >= 3.10.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=/etc/systemd/system/freelancer-bot.service
PY="${PYTHON:-python3}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

command -v "$PY" >/dev/null || { echo "python3 not found"; exit 1; }
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit("python 3.10+ required, found %s" % sys.version.split()[0])
EOF

say "installing into $APP_DIR"
mkdir -p "$APP_DIR/data" "$APP_DIR/secrets"
chmod 700 "$APP_DIR/secrets"

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  say "creating venv"
  "$PY" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" -q install --upgrade pip
"$APP_DIR/.venv/bin/pip" -q install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  warn "created .env — fill in FL_BOT_TOKEN and the Freelancer cookies, then restart"
fi

if [ ! -f "$APP_DIR/secrets/cookies.txt" ]; then
  warn "secrets/cookies.txt is missing: scanning will fail until you add the Freelancer cookies"
fi

say "writing $SERVICE"
cat > "$SERVICE" <<EOF
[Unit]
Description=Freelancer.com project bot (scan, draft, confirm, bid)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m freelancer_manager run
Restart=always
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable freelancer-bot >/dev/null

if [ "${1:-}" = "--no-start" ]; then
  say "installed; not started (--no-start)"
  exit 0
fi

systemctl restart freelancer-bot
sleep 3
systemctl --no-pager --lines=5 status freelancer-bot || true
say "done. Next: send /start to the bot in Telegram (binds the chat), then /ai to set the model key."
