#!/usr/bin/env python3
"""Once-a-day morning briefing: today's weather alongside today's air.

Runs from its own timer rather than piggybacking on the hourly watchdog,
because the two have opposite jobs. The watchdog is exception-driven and stays
silent when nothing is wrong; the briefing is unconditional and arrives whether
or not the air is bad. Sharing a schedule would force one of them to behave
like the other.

This process never writes the watchdog's state file. The hourly timer remains
its sole writer, so a briefing can never disturb band history or suppress an
alert.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.error
from datetime import datetime, timezone

from aqi_bot import (
    ADVICE,
    BANDS,
    ConfigError,
    ForecastPoint,
    Reading,
    band_index,
    esc,
    fetch_forecast_detail,
    get_reading,
    pause_until,
    require_env,
    send_telegram,
)
from aqi_weather import Weather, describe_code, fetch_weather

log = logging.getLogger("aqi-briefing")

# Thresholds at which a number stops being trivia and becomes something you
# would change your morning over.
WET_CHANCE_PERCENT = 60
WET_MILLIMETRES = 5.0
HARSH_UV_INDEX = 8.0
BREEZY_WIND_KMH = 30.0

FORECAST_HOURS = 24
MIN_REMAINING_POINTS = 3


def format_clock(stamp: object) -> str | None:
    """Render an ISO local timestamp as a bare '07:05', or None if unusable."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).strftime("%H:%M")
    except ValueError:
        return None


def greeting_for(moment: datetime | None) -> str:
    """Pick a greeting for the local time of day.

    The timer fires in the morning, but /briefing can be asked for at any
    hour, and "Good morning" at nine in the evening reads as a bug.
    """
    if moment is None:
        return "Good morning"
    if moment.hour < 12:
        return "Good morning"
    if moment.hour < 18:
        return "Good afternoon"
    return "Good evening"


def points_for_today(points: list[ForecastPoint], local_now: datetime | None) -> tuple[list[ForecastPoint], str]:
    """Narrow an hourly series to today, with a label describing the window.

    Late in the day "today" leaves too few hours to be worth reporting, so the
    window rolls forward instead of shrinking to a single sample.
    """
    if not points:
        return [], "today"

    if local_now is not None:
        today = local_now.date().isoformat()
        same_day = [point for point in points if point.time.startswith(today)]
        if len(same_day) >= MIN_REMAINING_POINTS:
            return same_day, "today"

    return points[:12], "the next 12 hours"


def describe_weather_block(weather: Weather) -> list[str]:
    """Render the weather half of the briefing as message lines."""
    current = weather.current
    today = weather.today
    lines: list[str] = []

    now_label, _emoji = describe_code(current.code)
    now_parts: list[str] = []
    if current.temperature is not None:
        now_parts.append(f"{current.temperature:.1f}°C")
    if current.apparent_temperature is not None:
        now_parts.append(f"feels {current.apparent_temperature:.1f}°C")
    now_parts.append(now_label)
    lines.append(f"Now: {', '.join(now_parts)}")

    day_label, _day_emoji = describe_code(today.code)
    if today.high is not None and today.low is not None:
        lines.append(f"Today: {today.low:.0f}°C to {today.high:.0f}°C, {day_label}")
    else:
        lines.append(f"Today: {day_label}")

    rain_parts: list[str] = []
    if today.rain_chance is not None:
        rain_parts.append(f"{today.rain_chance}% chance")
    if today.precipitation:
        rain_parts.append(f"{today.precipitation:.1f} mm")
    if rain_parts:
        lines.append(f"Rain: {', '.join(rain_parts)}")

    if today.uv_index is not None:
        lines.append(f"UV index: {today.uv_index:.0f} at peak")
    if current.humidity is not None:
        lines.append(f"Humidity: {current.humidity:.0f}%")

    sunrise = format_clock(today.sunrise)
    sunset = format_clock(today.sunset)
    if sunrise and sunset:
        lines.append(f"Sun: {sunrise} to {sunset}")

    return lines


def describe_air_block(reading: Reading | None, points: list[ForecastPoint], window: str) -> list[str]:
    """Render the air-quality half of the briefing as message lines."""
    lines: list[str] = []

    if reading is not None:
        band = band_index(reading.aqi)
        _lower, _upper, label, emoji = BANDS[band]
        lines.append(f"{emoji} Air: AQI {reading.aqi} — {label}")
        lines.append(f"Source: {esc(reading.source)}")
    else:
        lines.append("Air: no reading available this morning.")

    if points:
        peak = max(points, key=lambda point: point.aqi)
        peak_band = band_index(peak.aqi)
        peak_time = format_clock(peak.time) or peak.time
        lines.append(
            f"Peak {window}: {BANDS[peak_band][3]} AQI {peak.aqi} at {peak_time}"
        )

    return lines


def collect_advice(weather: Weather | None, reading: Reading | None, points: list[ForecastPoint]) -> list[str]:
    """Turn the numbers into the handful of things worth acting on.

    A briefing that only lists readings makes the reader do the interpretation
    every morning. Anything that clears a threshold earns one sentence; when
    nothing does, say so rather than padding.
    """
    advice: list[str] = []

    if weather is not None:
        today = weather.today
        wet = (today.rain_chance or 0) >= WET_CHANCE_PERCENT or (today.precipitation or 0) >= WET_MILLIMETRES
        if wet:
            advice.append("Take an umbrella.")
        if (today.uv_index or 0) >= HARSH_UV_INDEX:
            advice.append("UV is harsh around midday. Cover up outdoors.")
        if (weather.current.wind_speed or 0) >= BREEZY_WIND_KMH:
            advice.append("Windy enough to matter for anything loose outside.")

    # Plan against the worst the air is expected to get, not just the reading
    # at the moment the briefing happens to run.
    bands = [band_index(reading.aqi)] if reading is not None else []
    if points:
        bands.append(band_index(max(point.aqi for point in points)))
    if bands:
        worst = max(bands)
        if worst >= 2:
            advice.append(ADVICE[worst])

    if not advice:
        advice.append("Nothing to plan around. Ordinary day.")

    return advice


def compose_briefing(
    weather: Weather | None,
    reading: Reading | None,
    points: list[ForecastPoint],
    location: str,
    local_now: datetime | None,
) -> str:
    """Build the HTML briefing body from whichever halves were available."""
    _label, emoji = describe_code(weather.current.code if weather else None)
    headline = f"{emoji} <b>{greeting_for(local_now)} — {esc(location)}</b>"

    lines = [headline]
    if local_now is not None:
        lines.append(f"{local_now:%A}, {local_now.day} {local_now:%B}")
    lines.append("")

    if weather is not None:
        lines += describe_weather_block(weather)
    else:
        lines.append("Weather is unavailable this morning.")
    lines.append("")

    window_points, window = points_for_today(points, local_now)
    lines += describe_air_block(reading, window_points, window)
    lines.append("")

    lines += collect_advice(weather, reading, window_points)

    return "\n".join(lines).strip()


def gather(lat: float, lon: float, waqi_token: str | None) -> tuple[Weather | None, Reading | None, list[ForecastPoint]]:
    """Fetch all three inputs, tolerating any of them being unavailable."""
    weather = fetch_weather(lat, lon)
    if weather is None:
        log.warning("No weather available; briefing will run on air quality alone")

    try:
        reading: Reading | None = get_reading(lat, lon, waqi_token)
    except RuntimeError as exc:
        log.warning("No air-quality reading available: %s", exc)
        reading = None

    points = fetch_forecast_detail(lat, lon, FORECAST_HOURS).points

    return weather, reading, points


def main() -> int:
    """Send one briefing. Returns a process exit code."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(message)s",
    )

    try:
        telegram_token = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("BRIEFING_CHAT_ID", "").strip() or require_env("TELEGRAM_CHAT_ID")
        lat = float(require_env("AQI_LAT"))
        lon = float(require_env("AQI_LON"))
    except (ConfigError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 2

    paused = pause_until(os.environ.get("AQI_PAUSE_UNTIL"), datetime.now(timezone.utc))
    if paused:
        log.info("Paused until %s; no briefing today", paused.isoformat(timespec="minutes"))
        return 0

    waqi_token = os.environ.get("WAQI_TOKEN", "").strip() or None
    location = os.environ.get("AQI_LOCATION_NAME", f"{lat},{lon}")

    weather, reading, points = gather(lat, lon, waqi_token)

    # Half a briefing is still useful, but a briefing with nothing in it is
    # noise. Exit non-zero so systemd records the failed morning.
    if weather is None and reading is None and not points:
        log.error("Every source failed; sending nothing")
        return 1

    local_now = weather.current_local_time() if weather else None
    message = compose_briefing(weather, reading, points, location, local_now)

    try:
        send_telegram(telegram_token, chat_id, message)
    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
        log.error("Failed to send the briefing: %s", exc)
        return 1

    log.info("Briefing sent to %s", chat_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
