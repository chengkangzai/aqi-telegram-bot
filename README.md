# aqi-telegram-bot

An hourly air-quality watchdog that messages you on Telegram when the AQI where
you live crosses into a worse category — and again when it recovers.

It is deliberately small: one Python file, no third-party dependencies, a
systemd timer, and a JSON state file.

## Why band changes instead of hourly readings

A naive `if aqi > 100: notify` will message you every hour for the duration of a
haze episode, and you will mute it by day two. This bot tracks which **band** the
reading sits in and only speaks when the band changes:

| AQI | Band |
| --- | --- |
| 0–50 | Good |
| 51–100 | Moderate |
| 101–150 | Unhealthy for Sensitive Groups |
| 151–200 | Unhealthy |
| 201–300 | Very Unhealthy |
| 301+ | Hazardous |

A deadband (default 3 points) stops a reading hovering at a boundary from
alternating between two bands every hour. `AQI_ALERT_FLOOR_BAND` sets how bad
things must get before you hear anything at all — the default of `2` means the
first message arrives when AQI passes 100, and you also get one message when it
drops back below.

## Data sources

1. **WAQI / aqicn.org** — real ground-station measurements, including the
   Malaysian DOE APIMS network. Needs a free token from
   <https://aqicn.org/data-platform/token/>.
2. **Open-Meteo** — automatic fallback, no API key. This is CAMS *model* output
   rather than a measured reading, so treat it as an approximation.

If the WAQI station nearest you goes offline or returns a non-numeric AQI, the
bot falls back on its own and says which source it used in the message.

## Install

On any Debian/Ubuntu host with `python3` and systemd:

```bash
git clone https://github.com/chengkangzai/aqi-telegram-bot.git
cd aqi-telegram-bot
sudo ./deploy/install.sh
sudo $EDITOR /etc/aqi-bot/aqi-bot.env   # fill in token, chat id, coordinates
sudo systemctl start aqi-bot.service    # fire one check immediately
journalctl -u aqi-bot -n 30 --no-pager
```

`install.sh` creates an unprivileged `aqibot` service account, installs the
script to `/opt/aqi-bot`, and enables an hourly timer with `Persistent=true` so a
run missed while the host was down fires on the way back up.

## Configuration

See [`deploy/aqi-bot.env.example`](deploy/aqi-bot.env.example). Required:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AQI_LAT`, `AQI_LON`.

To find your chat id, message your bot, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
```

## Security notes

- The env file holds your bot token. `install.sh` writes it `0640 root:aqibot`.
  It is gitignored — never commit it.
- The bot uses **long polling semantics only** (it makes outbound calls; it never
  listens). No inbound port, no reverse proxy, no TLS certificate needed. It runs
  happily behind NAT with the firewall closed.
- The systemd unit is confined with `ProtectSystem=strict`, a syscall filter, and
  `RestrictAddressFamilies=AF_INET AF_INET6`.

## Tests

```bash
python3 -m unittest -v
```

Covers band boundaries, hysteresis, and message composition. No network needed.

## Licence

MIT
