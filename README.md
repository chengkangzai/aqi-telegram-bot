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

### Repeats while it stays bad

Silence is right when the air is fine, but during an active episode you want the
running commentary. So once the reading is at or above the alert floor, the bot
re-sends an update every `AQI_REPEAT_HOURS` (default 1) for as long as it stays
there, reporting how long it has been going:

> 🔴 **Still Unhealthy: AQI 174**
> Ongoing for 4 hours.

Set `AQI_REPEAT_HOURS=0` to go back to band-changes-only, or `=3` to be told
every three hours instead. Repeats never fire below the alert floor, so good air
still costs you nothing.

A worked example — a real-shaped episode at hourly checks produces 11 messages
across 19 hours, and none at all during the clean stretches at either end. See
`EpisodeScenarioTests` for the exact expectations.

## Talking to it

Alerts are pushed to you automatically, but you can also query on demand. The
listener registers these with Telegram, so they autocomplete in the `/` menu:

| Command | Does |
| --- | --- |
| `/now` | Fetch a fresh reading and reply immediately |
| `/forecast` | Hourly outlook for the next 24h. `/forecast 48` widens it (6–96h) |
| `/status` | Last completed check, current band, and active settings |
| `/where` | Which location and coordinates are being watched |
| `/help` | List the commands |

This runs as a second unit, `aqi-bot-listener.service`, using long polling — so
still no inbound port. It only ever *reads* the state file; the hourly timer
stays the sole writer, so the two cannot race, and a `/now` query never affects
whether an alert fires.

Only user ids in `TELEGRAM_ALLOWED_USER_IDS` (default: `TELEGRAM_CHAT_ID`) get a
response. Anyone else who finds the bot is logged and ignored.

The forecast always comes from Open-Meteo even when live readings use WAQI,
because WAQI only publishes a coarse daily PM2.5 outlook. It reports the
window's peak and trough, then samples the timeline down to eight rows so a
96-hour request stays as readable as a 24-hour one.

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
sudo systemctl start aqi-bot-listener   # start answering commands
journalctl -u aqi-bot -n 30 --no-pager
```

`install.sh` creates an unprivileged `aqibot` service account, installs the
script to `/opt/aqi-bot`, and enables an hourly timer with `Persistent=true` so a
run missed while the host was down fires on the way back up.

### Running in an unprivileged Proxmox LXC

Enable nesting on the container, or `systemd-journald` will not start and you
will get no logs at all:

```bash
pct set <vmid> --features nesting=1 && pct reboot <vmid>
```

Without it, journald dies with `status=243/CREDENTIALS` because systemd cannot
set up its credentials mount. The bot itself still runs — which is exactly what
makes this worth calling out, since the failure is silent.

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
