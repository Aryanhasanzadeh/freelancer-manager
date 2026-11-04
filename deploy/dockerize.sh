#!/usr/bin/env bash
# Move this host from the systemd/venv install to the container one. Idempotent.
#
#   sudo bash deploy/dockerize.sh
#
# The two installs must never run together: both poll getUpdates with the same
# token, so Telegram hands each update to only one of them and the other silently
# loses half the chats. This stops the unit, moves its file out of
# /etc/systemd/system, masks the name so a start fails, then hands data/ and
# secrets/ to the container user and starts the compose stack.
#
# Rollback:  rm /etc/systemd/system/freelancer-bot.service
#            cp deploy/disabled/freelancer-bot.service /etc/systemd/system/
#            systemctl daemon-reload && systemctl enable --now freelancer-bot
#            docker compose down
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

UNIT=freelancer-bot.service
# uid 10001 / gid 999 belong to the `bot` user built into the image.
RUN_UID=10001
RUN_GID=999

say() { printf '\033[1;36==>\033[0m %s\n' "$*"; }

# ------------------------------------------------------------------ 1. no more systemd
# `disable --now` is not enough: the unit file stays in /etc/systemd/system and one
# `systemctl start` — or anything that re-enables the preset — brings a second poller
# back beside the container. Take the file away and mask the name, which makes a
# start fail loudly. The original unit is kept for rollback.
LIB=/usr/lib/systemd/system/$UNIT
ETC=/etc/systemd/system/$UNIT
mkdir -p "$APP_DIR/deploy/disabled"
if [ -f "$ETC" ] && [ ! -L "$ETC" ]; then
    say "moving the host unit ($UNIT) out of /etc/systemd/system"
    systemctl disable --now "$UNIT" || true
    cp -f "$ETC" "$APP_DIR/deploy/disabled/$UNIT"
    rm -f "$ETC"
elif [ -f "$LIB" ] && ! systemctl is-enabled "$UNIT" >/dev/null 2>&1; then
    say "disabling the packaged unit ($LIB)"
    systemctl disable --now "$UNIT" || true
fi
if [ ! -L "$ETC" ]; then
    say "masking $UNIT so it cannot be started beside the container"
    systemctl daemon-reload
    systemctl mask "$UNIT" || true
fi
systemctl reset-failed "$UNIT" 2>/dev/null || true
systemctl daemon-reload

# A second poller is the one failure this script must not allow. Match the host venv
# on purpose: the container runs `python` from its own image, and pkill -f on the
# module name alone would also hit the container process (its cmdline looks identical).
HOST_POLLER="$APP_DIR/.venv/bin/python -m freelancer_manager"
if pgrep -af "$HOST_POLLER run" | grep -v -e docker -e pgrep; then
    say "killing a leftover host process from the old install"
    pkill -f "$HOST_POLLER run" || true
    sleep 2
fi
if systemctl is-active --quiet "$UNIT"; then
    say "REFUSING: $UNIT is still running — two pollers would split the updates"
    exit 1
fi

# ------------------------------------------------------------------ 2. state ownership
say "handing data/ and secrets/ to the container user"
mkdir -p data secrets
chown -R "$RUN_UID:$RUN_GID" data
# secrets stay root-owned and group-readable: the container can read them but
# cannot rewrite the token from inside.
chown -R root:"$RUN_GID" secrets
chmod 750 secrets
chmod 640 secrets/* 2>/dev/null || true
if [ ! -f .env ]; then
    say ".env missing — copy .env.example first"; exit 1
fi
grep -q '^FL_CHAT_ID=' .env || { say "FL_CHAT_ID missing from .env"; exit 1; }

# ------------------------------------------------------------------ 3. container up
say "building and starting the stack"
docker compose up -d --build --remove-orphans

say "waiting for the healthcheck"
for _ in $(seq 1 30); do
    state="$(docker inspect -f '{{.State.Health.Status}}' freelancer-bot 2>/dev/null || echo starting)"
    [ "$state" = healthy ] && break
    sleep 2
done
docker inspect -f '{{.State.Status}} health={{.State.Health.Status}}' freelancer-bot

if [ "${state:-}" != healthy ]; then
    say "container is not healthy — last logs:"
    docker compose logs --tail 40 bot
    exit 1
fi

say "container is up; the host unit is disabled so only one poller runs"
docker compose ps
