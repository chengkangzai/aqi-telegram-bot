#!/usr/bin/env bash
# Install the AQI watchdog onto a Debian host. Idempotent - safe to re-run.
set -euo pipefail

APP_DIR=/opt/aqi-bot
CONF_DIR=/etc/aqi-bot
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "==> Creating service account"
id -u aqibot &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin aqibot

echo "==> Installing application to ${APP_DIR}"
install -d -m 0755 "$APP_DIR"
install -m 0755 "${REPO_DIR}/aqi_bot.py" "${APP_DIR}/aqi_bot.py"

echo "==> Preparing ${CONF_DIR}"
install -d -m 0750 -o root -g aqibot "$CONF_DIR"
if [[ ! -f "${CONF_DIR}/aqi-bot.env" ]]; then
  install -m 0640 -o root -g aqibot "${REPO_DIR}/deploy/aqi-bot.env.example" "${CONF_DIR}/aqi-bot.env"
  echo "    Wrote a template env file. Edit it before starting the timer."
else
  echo "    Existing env file left untouched."
fi

echo "==> Installing systemd units"
install -m 0644 "${REPO_DIR}/deploy/aqi-bot.service" /etc/systemd/system/aqi-bot.service
install -m 0644 "${REPO_DIR}/deploy/aqi-bot.timer" /etc/systemd/system/aqi-bot.timer
systemctl daemon-reload
systemctl enable --now aqi-bot.timer

echo
echo "Done. Useful next steps:"
echo "  edit    \$EDITOR ${CONF_DIR}/aqi-bot.env"
echo "  test    systemctl start aqi-bot.service && journalctl -u aqi-bot -n 30 --no-pager"
echo "  status  systemctl list-timers aqi-bot.timer"
