# aqi-telegram-bot

An hourly air-quality watchdog that messages you on Telegram when the AQI where
you live crosses into a worse category — and again when it recovers, plus a
short weather-and-air briefing every morning.

It is deliberately small: standard-library Python only, systemd timers, and a
JSON state file. `matplotlib` is an optional extra, and only for charts.

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

## The morning briefing

At 10:00 local time every day the bot sends one short message covering the
weather and the air together, whether or not anything is wrong:

> 🌦️ **Good morning — Cheras**
> Wednesday, 9 September
>
> Now: 26.2°C, feels 31.7°C, Drizzle
> Today: 24°C to 35°C, Thunderstorm
> Rain: 100% chance, 20.8 mm
> UV index: 8 at peak
> Sun: 07:05 to 19:14
>
> 🟣 Air: AQI 218 — Very Unhealthy
> Peak today: 🟣 AQI 222 at 19:00
>
> Take an umbrella.
> UV is harsh around midday. Cover up outdoors.
> Avoid outdoor exertion. Keep windows shut and run a purifier.

This is the opposite job from the watchdog, which is why it is a separate unit
on a separate timer. The watchdog is exception-driven and says nothing when the
air is fine. The briefing is unconditional: you read it once, plan the day, and
throw it away.

The closing lines are the point. A list of numbers makes you do the
interpretation every morning, so anything that crosses a threshold earns one
plain sentence, and when nothing does it says *Nothing to plan around.*
The air advice follows the **forecast peak**, not just the reading at 10:00 —
clean air now and a bad afternoon is exactly the case where knowing early
changes what you do.

Weather comes from Open-Meteo's forecast API, keyless like the existing AQI
fallback, so the briefing adds no new credential. The two halves fail
independently: if the weather lookup dies you still get the air, and vice
versa. Only when everything fails does the run exit non-zero and send nothing.

The schedule lives in `deploy/aqi-bot-briefing.timer`:

```
OnCalendar=*-*-* 10:00:00 Asia/Kuala_Lumpur
```

The timezone is named there rather than inherited from the host clock, so the
briefing cannot drift because someone ran `timedatectl` on the container. Edit
the line to move the slot, then `systemctl daemon-reload`. Named timezones in
`OnCalendar` need systemd 252 or newer; `install.sh` checks and tells you what
to do instead if yours is older.

It is deliberately **not** `Persistent=true`. A briefing that fires at 16:00
because the host was asleep all morning is stale news, not a catch-up.

Send it to a different chat than the alerts by setting `BRIEFING_CHAT_ID`.
`/briefing` renders the same message on demand at any hour, and greets you
according to the actual time of day.

## Talking to it

Alerts are pushed to you automatically, but you can also query on demand. The
listener registers these with Telegram, so they autocomplete in the `/` menu:

| Command | Does |
| --- | --- |
| `/now` | Fetch a fresh reading and reply immediately |
| `/briefing` | Today's weather and air in one message, on demand |
| `/forecast` | Hourly outlook as a chart. `/forecast 48` widens it (6–96h) |
| `/status` | Last completed check, current band, and active settings |
| `/where` | Which location and coordinates are being watched |
| `/help` | List the commands |

This runs as a second unit, `aqi-bot-listener.service`, using long polling — so
still no inbound port. It only ever *reads* the state file; the hourly timer
stays the sole writer, so the two cannot race, and a `/now` query never affects
whether an alert fires.

Only user ids in `TELEGRAM_ALLOWED_USER_IDS` (default: `TELEGRAM_CHAT_ID`) get a
response. Anyone else who finds the bot is logged and ignored.

`/forecast` replies with a rendered PNG built around one split: the **headline**
owns *how bad is it* and the **chart** owns *how does it change*. Trying to make
the plot do both is what made earlier versions hard to read.

The headline gives the current reading large, in its band colour, with the band
name beside it (never colour alone), plus a plain-language sentence:

> **161**
> UNHEALTHY · RIGHT NOW
> Worsens to 217 in about 2 hours, then eases to 153 in about 6 hours (23:00).

Times are relative, since "around 19:00" makes the reader do arithmetic. Past
six hours a clock time is appended, because "in about 14 hours" is not
something you can picture. The verb tracks reality: a rise that stays inside one
band "rises" rather than "worsens", a flat window "holds steady", and the trough
is taken from after the peak so it never describes a value already in the past.

The chart is then free to stay zoomed and recessive — one neutral line over
labelled severity bands, anchored a band below the data rather than at zero,
with a solid rule marking now. `AQI_CHART_THEME=light|dark` picks the palette;
both are chosen against their own surface rather than one being an inversion.

Charting needs `python3-matplotlib` (the installer adds it). Without it, or if
rendering throws, `/forecast` degrades to the same summary in text — the reply
is never lost.

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
sudo systemctl start aqi-bot-briefing   # send a briefing now, to check it
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

## Pausing

Going away, or just need a quiet week? Set `AQI_PAUSE_UNTIL` in the env file:

```bash
AQI_PAUSE_UNTIL=2026-10-21T09:55+08:00   # or just 2026-10-21 (local midnight)
```

Hourly alerts and the morning briefing are skipped until then and resume on
their own afterwards — no timer to remember to re-enable. The listener keeps
answering `/now` and friends. A paused check fetches nothing and writes no
state, so the first run after the pause compares against the last band it
actually saw. An unreadable value is logged and ignored rather than obeyed.

## Setting secrets

Use the helper rather than editing the file by hand — the value is read from a
silent prompt, so it never lands in shell history, in `ps` output, or on screen:

```bash
sudo ./deploy/set-secret.sh WAQI_TOKEN
sudo ./deploy/set-secret.sh TELEGRAM_BOT_TOKEN
```

The hourly timer re-reads the env file every run; the listener caches at
startup, so the script restarts it for you.

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
