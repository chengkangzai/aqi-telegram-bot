#!/usr/bin/env python3
"""Hourly air-quality watchdog that alerts a Telegram chat on AQI band changes.

Reads the US EPA AQI for a fixed location, primarily from WAQI ground stations
with an Open-Meteo model fallback, and messages Telegram only when the reading
crosses into a different severity band. State is persisted between runs so a
multi-day pollution episode produces a handful of messages rather than one per
hour.
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

USER_AGENT = "aqi-telegram-bot/1.0 (+https://github.com/chengkangzai/aqi-telegram-bot)"
HTTP_TIMEOUT = 20

# US EPA AQI bands: (lower, upper, label, emoji).
BANDS: list[tuple[int, int, str, str]] = [
    (0, 50, "Good", "🟢"),
    (51, 100, "Moderate", "🟡"),
    (101, 150, "Unhealthy for Sensitive Groups", "🟠"),
    (151, 200, "Unhealthy", "🔴"),
    (201, 300, "Very Unhealthy", "🟣"),
    (301, 10_000, "Hazardous", "🟤"),
]

ADVICE = {
    0: "Air quality is fine. Windows open.",
    1: "Acceptable. Unusually sensitive people may want to take it easy outdoors.",
    2: "Sensitive groups should cut back on prolonged outdoor exertion.",
    3: "Everyone should limit prolonged outdoor exertion. Consider a mask outside.",
    4: "Avoid outdoor exertion. Keep windows shut and run a purifier.",
    5: "Stay indoors. Seal up and purify.",
}

log = logging.getLogger("aqi-bot")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass
class Reading:
    """A single air-quality observation normalised across providers."""

    aqi: int
    source: str
    station: str | None = None
    dominant: str | None = None
    observed_at: str | None = None
    pm25: float | None = None


@dataclass
class ForecastPoint:
    """A single hourly forecast sample."""

    time: str
    aqi: int


@dataclass
class Forecast:
    """Hourly samples plus the provider's own idea of "now".

    The timestamp comes from the same response as the samples and is in the
    location's timezone, so nothing downstream has to trust the host clock.
    """

    points: list[ForecastPoint]
    now: str | None = None
    utc_offset_seconds: int | None = None

    def current_local_time(self) -> datetime | None:
        """The current moment in the location's timezone, to the minute.

        The provider's own `now` is rounded to the hour, which would put a
        "now" marker exactly on the first sample. Reconstructing it from UTC
        plus the reported offset puts the marker where the time actually is,
        and still never consults the host's timezone.
        """
        if self.utc_offset_seconds is None:
            return None
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        return utc_now + timedelta(seconds=self.utc_offset_seconds)


def esc(value: object) -> str:
    """Escape a value for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def band_index(aqi: int) -> int:
    """Return the index into BANDS that the given AQI falls into."""
    for index, (lower, upper, _label, _emoji) in enumerate(BANDS):
        if lower <= aqi <= upper:
            return index
    return len(BANDS) - 1


def http_get_json(url: str) -> dict:
    """GET a URL and decode the JSON body."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_waqi(lat: float, lon: float, token: str) -> Reading | None:
    """Fetch the nearest WAQI ground-station reading, or None if unavailable."""
    url = f"https://api.waqi.info/feed/geo:{lat};{lon}/?token={urllib.parse.quote(token)}"
    try:
        payload = http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning("WAQI request failed: %s", exc)
        return None

    if payload.get("status") != "ok":
        log.warning("WAQI returned status=%s data=%s", payload.get("status"), payload.get("data"))
        return None

    data = payload.get("data") or {}
    aqi = data.get("aqi")
    if not isinstance(aqi, int):
        log.warning("WAQI returned a non-numeric AQI (%r) - station likely offline", aqi)
        return None

    pm25 = (data.get("iaqi") or {}).get("pm25", {}).get("v")
    return Reading(
        aqi=aqi,
        source="WAQI ground station",
        station=(data.get("city") or {}).get("name"),
        dominant=data.get("dominentpol"),
        observed_at=(data.get("time") or {}).get("s"),
        pm25=pm25 if isinstance(pm25, (int, float)) else None,
    )


def fetch_open_meteo(lat: float, lon: float) -> Reading | None:
    """Fetch modelled AQI from Open-Meteo (no API key), or None if unavailable."""
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "us_aqi,pm2_5",
            "timezone": "auto",
        }
    )
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?{query}"
    try:
        payload = http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning("Open-Meteo request failed: %s", exc)
        return None

    current = payload.get("current") or {}
    aqi = current.get("us_aqi")
    if aqi is None:
        log.warning("Open-Meteo response carried no us_aqi field")
        return None

    return Reading(
        aqi=int(round(float(aqi))),
        source="Open-Meteo (modelled)",
        observed_at=current.get("time"),
        pm25=current.get("pm2_5"),
    )


def fetch_forecast(lat: float, lon: float, hours: int = 24) -> list[ForecastPoint]:
    """Fetch the hourly US AQI forecast, discarding the current-time marker."""
    return fetch_forecast_detail(lat, lon, hours).points


def fetch_forecast_detail(lat: float, lon: float, hours: int = 24) -> Forecast:
    """Fetch the hourly US AQI forecast from Open-Meteo.

    WAQI only publishes a coarse daily PM2.5 outlook, so the forecast always
    comes from Open-Meteo regardless of which source the live reading used.
    Times are returned in the location's own timezone.
    """
    forecast_days = max(1, min(5, (hours // 24) + 2))
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "us_aqi",
            "current": "us_aqi",
            "timezone": "auto",
            "forecast_days": forecast_days,
        }
    )
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?{query}"
    try:
        payload = http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning("Forecast request failed: %s", exc)
        return Forecast(points=[])

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("us_aqi") or []
    now_stamp = (payload.get("current") or {}).get("time")

    # The response starts at midnight local, so skip anything already past.
    start = 0
    if now_stamp:
        for index, stamp in enumerate(times):
            if stamp >= now_stamp:
                start = index
                break

    points: list[ForecastPoint] = []
    for stamp, value in zip(times[start:], values[start:]):
        if value is None:
            continue
        points.append(ForecastPoint(time=stamp, aqi=int(round(float(value)))))
        if len(points) >= hours:
            break
    offset = payload.get("utc_offset_seconds")
    return Forecast(
        points=points,
        now=now_stamp,
        utc_offset_seconds=int(offset) if isinstance(offset, (int, float)) else None,
    )


def get_reading(lat: float, lon: float, waqi_token: str | None) -> Reading:
    """Return a reading from WAQI, falling back to Open-Meteo."""
    if waqi_token:
        reading = fetch_waqi(lat, lon, waqi_token)
        if reading is not None:
            return reading
        log.info("Falling back to Open-Meteo")
    else:
        log.info("No WAQI token configured; using Open-Meteo")

    reading = fetch_open_meteo(lat, lon)
    if reading is None:
        raise RuntimeError("Both WAQI and Open-Meteo failed to return a reading")
    return reading


def settle_band(aqi: int, previous_band: int | None, deadband: int) -> int:
    """Resolve the effective band, resisting flapping across a boundary.

    A move to a new band is only accepted once the reading clears the boundary
    by `deadband` points, so an AQI oscillating around 100 does not alternate
    between Moderate and Unhealthy every hour.
    """
    raw = band_index(aqi)
    if previous_band is None or raw == previous_band:
        return raw

    if raw > previous_band:
        lower_bound = BANDS[raw][0]
        return raw if aqi >= lower_bound + deadband else previous_band

    upper_bound = BANDS[raw][1]
    return raw if aqi <= upper_bound - deadband else previous_band


def parse_timestamp(value: object) -> datetime | None:
    """Parse a stored ISO timestamp, returning None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def repeat_is_due(last_alert_at: object, repeat_hours: int, now: datetime) -> bool:
    """Decide whether enough time has passed to re-send an ongoing alert.

    A five-minute tolerance absorbs the timer's randomised delay, which would
    otherwise make an "hourly" repeat skip to every second hour whenever a run
    landed a minute early.
    """
    if repeat_hours <= 0:
        return False
    last_alert = parse_timestamp(last_alert_at)
    if last_alert is None:
        return True
    elapsed = (now - last_alert).total_seconds()
    return elapsed >= repeat_hours * 3600 - 300


def load_state(path: str) -> dict:
    """Read persisted state, returning an empty dict when absent or corrupt."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read state file %s (%s); starting fresh", path, exc)
        return {}


def save_state(path: str, state: dict) -> None:
    """Persist state atomically so an interrupted run cannot corrupt it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(temporary, path)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Post a message to Telegram, raising on a non-ok response."""
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {payload}")


def humanise_duration(seconds: float) -> str:
    """Render an elapsed span as a short human phrase."""
    hours = int(seconds // 3600)
    if hours < 1:
        return "under an hour"
    if hours == 1:
        return "1 hour"
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def send_photo(token: str, chat_id: str, photo: bytes, caption: str) -> None:
    """Upload a PNG to Telegram with a caption, raising on a non-ok response."""
    boundary = "----aqibotformboundary"
    parts = b""
    for name, value in (("chat_id", chat_id), ("caption", caption[:1024]), ("parse_mode", "HTML")):
        parts += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    parts += (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="photo"; filename="forecast.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("utf-8")
    parts += photo + f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=parts,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT * 2) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected the photo: {payload}")


def relative_phrase(target: datetime, now: datetime | None) -> str:
    """Describe when `target` happens relative to `now`, in plain language.

    Falls back to a clock time when `now` is unknown, and appends one for
    anything far enough out that "in about 18 hours" is hard to picture.
    """
    if now is None:
        return f"at {target:%H:%M}"

    seconds = (target - now).total_seconds()
    if seconds <= 600:
        return "right about now"

    minutes = seconds / 60
    if minutes < 50:
        return f"in about {int(round(minutes / 10) * 10)} minutes"
    hours = minutes / 60
    if hours < 1.5:
        return "in about an hour"
    if hours < 22:
        phrase = f"in about {int(round(hours))} hours"
    elif hours < 36:
        phrase = "in about a day"
    else:
        phrase = f"in about {int(round(hours / 24))} days"

    # Past a few hours the relative form alone stops being concrete. Past a day
    # a bare clock time is ambiguous too - "(20:00)" does not say which day.
    if hours > 22:
        return f"{phrase} ({target:%a %H:%M})"
    if hours > 6:
        return f"{phrase} ({target:%H:%M})"
    return phrase


def compose_message(
    reading: Reading,
    band: int,
    previous_band: int | None,
    location: str,
    repeat: bool = False,
    band_age_seconds: float | None = None,
) -> str:
    """Build the HTML message body for a band transition or an ongoing update."""
    _lower, _upper, label, emoji = BANDS[band]

    if repeat:
        headline = f"{emoji} <b>Still {label}: AQI {reading.aqi}</b>"
        if band_age_seconds is not None:
            movement = f"Ongoing for {humanise_duration(band_age_seconds)}."
        else:
            movement = "Still elevated."
    elif previous_band is None:
        headline = f"{emoji} <b>AQI {reading.aqi}</b> — {label}"
        movement = "Monitoring started."
    elif band > previous_band:
        headline = f"{emoji} <b>Air quality worsening: AQI {reading.aqi}</b> — {label}"
        movement = f"Up from {BANDS[previous_band][2]}."
    else:
        headline = f"{emoji} <b>Air quality improving: AQI {reading.aqi}</b> — {label}"
        movement = f"Down from {BANDS[previous_band][2]}."

    lines = [headline, "", movement, ADVICE.get(band, ""), ""]

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


def require_env(name: str) -> str:
    """Read a required environment variable or raise ConfigError."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def main() -> int:
    """Run one check cycle. Returns a process exit code."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(message)s",
    )

    try:
        telegram_token = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = require_env("TELEGRAM_CHAT_ID")
        lat = float(require_env("AQI_LAT"))
        lon = float(require_env("AQI_LON"))
    except (ConfigError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 2

    waqi_token = os.environ.get("WAQI_TOKEN", "").strip() or None
    location = os.environ.get("AQI_LOCATION_NAME", f"{lat},{lon}")
    state_path = os.environ.get("AQI_STATE_FILE", "/var/lib/aqi-bot/state.json")
    alert_floor = int(os.environ.get("AQI_ALERT_FLOOR_BAND", "2"))
    deadband = int(os.environ.get("AQI_DEADBAND", "3"))
    repeat_hours = int(os.environ.get("AQI_REPEAT_HOURS", "1"))

    try:
        reading = get_reading(lat, lon, waqi_token)
    except RuntimeError as exc:
        # A failed fetch is expected occasionally on a flaky uplink. Exit
        # non-zero so systemd records it, but never write state.
        log.error("%s", exc)
        return 1

    now = datetime.now(timezone.utc)
    state = load_state(state_path)
    previous_band = state.get("band")
    if not isinstance(previous_band, int):
        previous_band = None

    band = settle_band(reading.aqi, previous_band, deadband)
    band_changed = band != previous_band

    # Track how long we have been sitting in this band, so an ongoing alert can
    # say "for 5 hours" rather than repeating itself blankly.
    band_since = now if band_changed else (parse_timestamp(state.get("band_since")) or now)

    log.info(
        "AQI %s -> band %s (%s), previous %s, source %s",
        reading.aqi,
        band,
        BANDS[band][2],
        previous_band,
        reading.source,
    )

    # Two independent reasons to speak:
    #   crossing - moved into or out of the zone worth reporting
    #   repeat   - still in a bad band, and the repeat interval has elapsed
    crossing = band_changed and (band >= alert_floor or (previous_band or 0) >= alert_floor)
    repeat = (
        not crossing
        and band >= alert_floor
        and repeat_is_due(state.get("last_alert_at"), repeat_hours, now)
    )

    if crossing or repeat:
        message = compose_message(
            reading,
            band,
            previous_band,
            location,
            repeat=repeat,
            band_age_seconds=(now - band_since).total_seconds() if repeat else None,
        )
        try:
            send_telegram(telegram_token, chat_id, message)
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            # Do not persist anything, so the next run retries the alert.
            log.error("Failed to send Telegram message: %s", exc)
            return 1
        state["last_alert_at"] = now.isoformat(timespec="seconds")
        log.info("Alert sent (%s): band %s -> %s", "repeat" if repeat else "change", previous_band, band)
    else:
        log.info("No alert needed")

    state.update(
        {
            "band": band,
            "aqi": reading.aqi,
            "source": reading.source,
            "band_since": band_since.isoformat(timespec="seconds"),
            "checked_at": now.isoformat(timespec="seconds"),
        }
    )
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
