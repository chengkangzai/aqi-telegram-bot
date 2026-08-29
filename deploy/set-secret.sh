#!/usr/bin/env bash
# Set a secret in the bot's env file without it appearing on the command line,
# in shell history, or on screen.
#
#   sudo ./deploy/set-secret.sh WAQI_TOKEN
#   sudo ./deploy/set-secret.sh TELEGRAM_BOT_TOKEN
#
# The value is read from a silent prompt, so it is never an argument (visible in
# `ps`), never echoed, and never written to history.
set -euo pipefail

CONF=/etc/aqi-bot/aqi-bot.env
KEY="${1:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ -z "$KEY" ]]; then
  echo "Usage: $0 <ENV_KEY>    e.g. $0 WAQI_TOKEN" >&2
  exit 1
fi
if [[ ! "$KEY" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
  echo "Key must be UPPER_SNAKE_CASE." >&2
  exit 1
fi
if [[ ! -f "$CONF" ]]; then
  echo "No config at $CONF — run install.sh first." >&2
  exit 1
fi

read -rsp "Value for ${KEY} (input hidden): " VALUE
echo

if [[ -z "$VALUE" ]]; then
  echo "Empty value; nothing changed." >&2
  exit 1
fi

# Rewrite via a temp file with the same ownership, so the secret is never in a
# world-readable intermediate.
TMP="$(mktemp)"
chmod 0640 "$TMP"
chown --reference="$CONF" "$TMP" 2>/dev/null || true

if grep -qE "^${KEY}=" "$CONF"; then
  # Use awk rather than sed -i so the value is never parsed as a sed expression
  # (a token containing / or & would otherwise corrupt the file).
  awk -v key="$KEY" -v val="$VALUE" \
    'BEGIN{FS=OFS="="} $1==key {print key "=" val; found=1; next} {print} END{if(!found) print key "=" val}' \
    "$CONF" > "$TMP"
else
  cp "$CONF" "$TMP"
  printf '%s=%s\n' "$KEY" "$VALUE" >> "$TMP"
fi

mv "$TMP" "$CONF"
chmod 0640 "$CONF"
chown root:aqibot "$CONF" 2>/dev/null || true
unset VALUE

echo "${KEY} updated (${#KEY} char key, value hidden)."

# The timer re-reads the file each run; the listener caches at startup.
if systemctl is-active --quiet aqi-bot-listener.service; then
  systemctl restart aqi-bot-listener.service
  echo "Listener restarted so it picks up the new value."
fi

echo
echo "Verify without revealing it:"
echo "  sudo systemctl start aqi-bot.service && journalctl -u aqi-bot -n 5 --no-pager"
