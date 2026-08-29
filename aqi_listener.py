#!/usr/bin/env python3
"""Long-polling Telegram listener that answers on-demand air-quality queries.

Runs alongside (not instead of) the hourly watchdog. This process only ever
reads the shared state file; the timer remains the sole writer, so the two
cannot race. Queries here never influence whether an alert fires.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime

from aqi_bot import (
    ADVICE,
    BANDS,
    ForecastPoint,
    Reading,
    band_index,
    esc,
    fetch_forecast_detail,
    get_reading,
    load_state,
    send_photo,
    send_telegram,
)

DEFAULT_FORECAST_HOURS = 24
MIN_FORECAST_HOURS = 6
MAX_FORECAST_HOURS = 96
FORECAST_ROWS = 8


def render_chart(points, location, theme, now=None):
    """Render a forecast chart, returning None if charting is unavailable.

    matplotlib is an optional extra: without it the bot still answers, just in
    text. Import lazily so the alerting path never pays for it.
    """
    try:
        from aqi_chart import render_forecast_chart
    except ImportError:
        log.info("matplotlib not installed; sending the forecast as text")
        return None
    try:
        return render_forecast_chart(points, location, theme, now=now)
    except Exception:  # noqa: BLE001 - a chart failure must not lose the reply
        log.exception("Chart rendering failed; falling back to text")
        return None

POLL_TIMEOUT = 30
HTTP_TIMEOUT = POLL_TIMEOUT + 15
BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120

COMMANDS = [
    ("now", "Current air quality reading"),
    ("forecast", "Hourly outlook, e.g. /forecast 48"),
    ("status", "Last check, band and settings"),
    ("where", "Which location is being watched"),
    ("help", "Show available commands"),
]

log = logging.getLogger("aqi-listener")
_running = True


def _stop(signum: int, _frame: object) -> None:
    """Flip the run flag so the poll loop exits cleanly."""
    global _running
    log.info("Received signal %s, shutting down", signum)
    _running = False


def api_call(token: str, method: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT) -> dict:
    """Call a Telegram Bot API method and return the decoded response."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def register_commands(token: str) -> None:
    """Publish the command list so Telegram offers autocomplete."""
    payload = [{"command": name, "description": desc} for name, desc in COMMANDS]
    try:
        api_call(token, "setMyCommands", {"commands": json.dumps(payload)}, timeout=20)
        log.info("Registered %d commands with Telegram", len(payload))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.warning("Could not register commands: %s", exc)


def load_offset(path: str) -> int:
    """Read the last processed update id, or 0 when starting fresh."""
    try:
        with open(path, encoding="utf-8") as handle:
            return int(json.load(handle).get("offset", 0))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def save_offset(path: str, offset: int) -> None:
    """Persist the update offset so restarts do not replay old messages."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"offset": offset}, handle)
    os.replace(temporary, path)


def describe_reading(reading: Reading, location: str) -> str:
    """Render a full current-conditions reply."""
    band = band_index(reading.aqi)
    _lower, _upper, label, emoji = BANDS[band]

    lines = [
        f"{emoji} <b>AQI {reading.aqi}</b> — {label}",
        "",
        ADVICE.get(band, ""),
        "",
    ]
    if reading.pm25 is not None:
        lines.append(f"PM2.5: {reading.pm25}")
    if reading.dominant:
        lines.append(f"Dominant pollutant: {esc(reading.dominant)}")
    lines.append(f"Location: {esc(location)}")
    if reading.station:
        lines.append(f"Station: {esc(reading.station)}")
    lines.append(f"Source: {esc(reading.source)}")
    if reading.observed_at:
        lines.append(f"Observed: {esc(reading.observed_at)}")
    return "\n".join(lines).strip()


def describe_status(config: dict) -> str:
    """Render the watchdog's own state and settings."""
    state = load_state(config["state_path"])
    lines = ["<b>Watchdog status</b>", ""]

    band = state.get("band")
    if isinstance(band, int) and 0 <= band < len(BANDS):
        _lower, _upper, label, emoji = BANDS[band]
        lines.append(f"Last reading: {emoji} AQI {state.get('aqi', '?')} — {label}")
        lines.append(f"Source: {esc(state.get('source', 'unknown'))}")
        lines.append(f"Checked: {esc(state.get('checked_at', 'never'))} UTC")
    else:
        lines.append("No check has completed yet.")

    floor = config["alert_floor"]
    floor_label = BANDS[floor][2] if 0 <= floor < len(BANDS) else str(floor)
    lines += [
        "",
        f"Watching: {esc(config['location'])}",
        f"Alerting from: {floor_label} and worse",
        f"Deadband: {config['deadband']} AQI points",
        f"Primary source: {'WAQI ground station' if config['waqi_token'] else 'Open-Meteo (no WAQI token set)'}",
        "Checks run hourly.",
    ]
    return "\n".join(lines)


def describe_help() -> str:
    """Render the command list."""
    lines = ["<b>Commands</b>", ""]
    lines += [f"/{name} — {desc}" for name, desc in COMMANDS]
    lines += ["", "Alerts arrive automatically when the AQI band changes; you do not need to poll."]
    return "\n".join(lines)


def format_slot(stamp: str) -> str:
    """Render an ISO local timestamp as a short 'Fri 15:00' label."""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return moment.strftime("%a %H:%M")


def describe_forecast(points: list[ForecastPoint], location: str, hours: int) -> str:
    """Render an hourly outlook with its peak, trough and a sampled timeline."""
    if not points:
        return "Could not fetch a forecast just now. Try again shortly."

    peak = max(points, key=lambda point: point.aqi)
    trough = min(points, key=lambda point: point.aqi)

    def label(point: ForecastPoint) -> str:
        band = band_index(point.aqi)
        return f"{BANDS[band][3]} AQI {point.aqi} — {format_slot(point.time)}"

    lines = [
        f"📈 <b>Forecast — {esc(location)}</b>",
        f"Next {len(points)} hours",
        "",
        f"Peak:  {label(peak)}",
        f"Best:  {label(trough)}",
        "",
    ]

    # Sample evenly so the timeline stays readable at any window length.
    step = max(1, len(points) // FORECAST_ROWS)
    for point in points[::step][:FORECAST_ROWS]:
        band = band_index(point.aqi)
        lines.append(f"{format_slot(point.time)}  {BANDS[band][3]} {point.aqi}")

    worst_band = band_index(peak.aqi)
    lines += ["", ADVICE.get(worst_band, ""), "", "Source: Open-Meteo (modelled)"]
    return "\n".join(lines).strip()


def parse_forecast_hours(args: list[str]) -> int:
    """Read an optional hour count from the command arguments."""
    if not args:
        return DEFAULT_FORECAST_HOURS
    try:
        requested = int(args[0])
    except ValueError:
        return DEFAULT_FORECAST_HOURS
    return max(MIN_FORECAST_HOURS, min(MAX_FORECAST_HOURS, requested))


def extract_args(text: str) -> list[str]:
    """Return the whitespace-separated arguments following a command."""
    parts = text.strip().split()
    return parts[1:] if len(parts) > 1 else []


def handle_command(
    command: str, config: dict, args: list[str] | None = None
) -> tuple[str, bytes | None]:
    """Map a command word to its reply text and optional chart image."""
    if command in ("start", "help"):
        return describe_help(), None

    if command == "where":
        return (
            f"Watching <b>{esc(config['location'])}</b>\n"
            f"Coordinates: {config['lat']}, {config['lon']}"
        ), None

    if command == "status":
        return describe_status(config), None

    if command == "forecast":
        hours = parse_forecast_hours(args or [])
        forecast = fetch_forecast_detail(config["lat"], config["lon"], hours)
        points = forecast.points
        text = describe_forecast(points, config["location"], hours)
        chart = (
            render_chart(
                points,
                config["location"],
                config["chart_theme"],
                now=forecast.current_local_time(),
            )
            if points
            else None
        )
        return text, chart

    if command == "now":
        try:
            reading = get_reading(config["lat"], config["lon"], config["waqi_token"])
        except RuntimeError as exc:
            log.warning("/now failed: %s", exc)
            return "Could not reach any air-quality source just now. Try again shortly.", None
        return describe_reading(reading, config["location"]), None

    return f"Unknown command /{esc(command)}. Try /help.", None


def extract_command(text: str) -> str | None:
    """Pull a bare command word out of message text, or None."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    word = text.split()[0][1:]
    # Telegram appends @botname in groups.
    return word.split("@")[0].lower() or None


def process_update(update: dict, config: dict) -> None:
    """Act on a single Telegram update."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    sender = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text") or ""

    if sender not in config["allowed_users"]:
        log.warning("Ignoring message from unauthorised user id %s", sender)
        return

    command = extract_command(text)
    if command is None:
        log.info("Ignoring non-command message from %s", sender)
        return

    log.info("Handling /%s from %s", command, sender)
    reply, chart = handle_command(command, config, extract_args(text))
    try:
        if chart:
            send_photo(config["token"], str(chat_id), chart, reply)
        else:
            send_telegram(config["token"], str(chat_id), reply)
    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
        log.error("Failed to reply to /%s: %s", command, exc)


def build_config() -> dict:
    """Assemble runtime configuration from the environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    raw_allowed = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip() or chat_id
    allowed = set()
    for part in raw_allowed.split(","):
        part = part.strip()
        if part:
            try:
                allowed.add(int(part))
            except ValueError:
                log.warning("Ignoring non-numeric allowed user id %r", part)
    if not allowed:
        raise SystemExit("No valid entries in TELEGRAM_ALLOWED_USER_IDS")

    return {
        "token": token,
        "chat_id": chat_id,
        "allowed_users": allowed,
        "lat": float(os.environ["AQI_LAT"]),
        "lon": float(os.environ["AQI_LON"]),
        "location": os.environ.get("AQI_LOCATION_NAME", "your location"),
        "waqi_token": os.environ.get("WAQI_TOKEN", "").strip() or None,
        "state_path": os.environ.get("AQI_STATE_FILE", "/var/lib/aqi-bot/state.json"),
        "offset_path": os.environ.get("AQI_OFFSET_FILE", "/var/lib/aqi-bot/offset.json"),
        "chart_theme": os.environ.get("AQI_CHART_THEME", "light").strip().lower(),
        "alert_floor": int(os.environ.get("AQI_ALERT_FLOOR_BAND", "2")),
        "deadband": int(os.environ.get("AQI_DEADBAND", "3")),
    }


def main() -> int:
    """Poll Telegram for commands until signalled to stop."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config = build_config()
    register_commands(config["token"])

    offset = load_offset(config["offset_path"])
    log.info("Listening from offset %s, authorised users: %s", offset, sorted(config["allowed_users"]))

    backoff = BACKOFF_SECONDS
    while _running:
        try:
            response = api_call(
                config["token"],
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message"])},
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            # A flaky uplink is expected here; keep the loop alive.
            log.warning("getUpdates failed (%s); retrying in %ss", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue

        backoff = BACKOFF_SECONDS

        if not response.get("ok"):
            log.error("Telegram returned an error: %s", response)
            time.sleep(BACKOFF_SECONDS)
            continue

        for update in response.get("result", []):
            offset = max(offset, update.get("update_id", 0) + 1)
            try:
                process_update(update, config)
            except Exception:  # noqa: BLE001 - one bad update must not kill the loop
                log.exception("Error while processing update %s", update.get("update_id"))
            save_offset(config["offset_path"], offset)

    log.info("Listener stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
