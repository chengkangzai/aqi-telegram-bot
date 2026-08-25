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
from datetime import datetime, timezone

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


def compose_message(reading: Reading, band: int, previous_band: int | None, location: str) -> str:
    """Build the HTML message body for a band transition."""
    _lower, _upper, label, emoji = BANDS[band]

    if previous_band is None:
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

    try:
        reading = get_reading(lat, lon, waqi_token)
    except RuntimeError as exc:
        # A failed fetch is expected occasionally on a flaky uplink. Exit
        # non-zero so systemd records it, but never write state.
        log.error("%s", exc)
        return 1

    state = load_state(state_path)
    previous_band = state.get("band")
    if not isinstance(previous_band, int):
        previous_band = None

    band = settle_band(reading.aqi, previous_band, deadband)
    log.info(
        "AQI %s -> band %s (%s), previous %s, source %s",
        reading.aqi,
        band,
        BANDS[band][2],
        previous_band,
        reading.source,
    )

    # Alert when crossing into or out of the "worth telling me about" zone,
    # and on every band change while inside it.
    should_alert = band != previous_band and (band >= alert_floor or (previous_band or 0) >= alert_floor)

    if should_alert:
        message = compose_message(reading, band, previous_band, location)
        try:
            send_telegram(telegram_token, chat_id, message)
            log.info("Alert sent: band %s -> %s", previous_band, band)
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            # Do not persist the new band, so the next run retries the alert.
            log.error("Failed to send Telegram message: %s", exc)
            return 1
    else:
        log.info("No alert needed")

    state.update(
        {
            "band": band,
            "aqi": reading.aqi,
            "source": reading.source,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
