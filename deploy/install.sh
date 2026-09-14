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
install -m 0755 "${REPO_DIR}/aqi_listener.py" "${APP_DIR}/aqi_listener.py"
install -m 0755 "${REPO_DIR}/aqi_chart.py" "${APP_DIR}/aqi_chart.py"
install -m 0755 "${REPO_DIR}/aqi_weather.py" "${APP_DIR}/aqi_weather.py"
install -m 0755 "${REPO_DIR}/aqi_briefing.py" "${APP_DIR}/aqi_briefing.py"

echo "==> Checking chart support"
if python3 -c "import matplotlib" 2>/dev/null; then
  echo "    matplotlib present."
elif command -v apt-get >/dev/null; then
  echo "    Installing python3-matplotlib for /forecast charts..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-matplotlib >/dev/null 2>&1 \
    && echo "    Installed." \
    || echo "    Install failed - /forecast will reply in text instead."
else
  echo "    No matplotlib and no apt-get; /forecast will reply in text instead."
fi
if command -v apt-get >/dev/null && ! dpkg -s fonts-ibm-plex &>/dev/null; then
  echo "    Installing fonts-ibm-plex for chart typography..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fonts-ibm-plex >/dev/null 2>&1 \
    && echo "    Installed." \
    || echo "    Install failed - charts will use DejaVu Sans instead."
fi

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
install -m 0644 "${REPO_DIR}/deploy/aqi-bot-listener.service" /etc/systemd/system/aqi-bot-listener.service
install -m 0644 "${REPO_DIR}/deploy/aqi-bot-briefing.service" /etc/systemd/system/aqi-bot-briefing.service
install -m 0644 "${REPO_DIR}/deploy/aqi-bot-briefing.timer" /etc/systemd/system/aqi-bot-briefing.timer

echo "==> Checking the briefing schedule"
BRIEF_CAL="$(sed -n 's/^OnCalendar=//p' "${REPO_DIR}/deploy/aqi-bot-briefing.timer")"
if systemd-analyze calendar "$BRIEF_CAL" >/dev/null 2>&1; then
  echo "    Next briefing: $(systemd-analyze calendar "$BRIEF_CAL" | sed -n 's/^ *Next elapse: //p')"
else
  echo "    WARNING: this systemd cannot parse '${BRIEF_CAL}'." >&2
  echo "    Named timezones in OnCalendar need systemd 252+ (Debian 12+)." >&2
  echo "    Drop the timezone from the OnCalendar line and set the host clock instead:" >&2
  echo "      timedatectl set-timezone Asia/Kuala_Lumpur" >&2
fi

systemctl daemon-reload
systemctl enable --now aqi-bot.timer
systemctl enable --now aqi-bot-briefing.timer
systemctl enable aqi-bot-listener.service

echo
echo "Done. Useful next steps:"
echo "  edit    \$EDITOR ${CONF_DIR}/aqi-bot.env"
echo "  test    systemctl start aqi-bot.service && journalctl -u aqi-bot -n 30 --no-pager"
echo "  brief   systemctl start aqi-bot-briefing.service && journalctl -u aqi-bot-briefing -n 30 --no-pager"
echo "  status  systemctl list-timers 'aqi-bot*'"
echo "  listener systemctl start aqi-bot-listener && journalctl -u aqi-bot-listener -f"
