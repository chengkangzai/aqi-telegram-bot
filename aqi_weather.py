#!/usr/bin/env python3
"""Weather lookup for the morning briefing, from Open-Meteo.

Kept separate from `aqi_bot` because the two answer different questions and
fail independently: a briefing is still worth sending when one half is
missing. This uses the same keyless Open-Meteo family the AQI fallback already
relies on, so the briefing introduces no new credential to manage.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aqi_bot import http_get_json

log = logging.getLogger("aqi-weather")

# WMO weather interpretation codes, as returned by Open-Meteo.
WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Freezing fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌦️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Heavy freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "❄️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"),
    81: ("Showers", "🌧️"),
    82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with hail", "⛈️"),
    99: ("Thunderstorm with heavy hail", "⛈️"),
}


def describe_code(code: int | None) -> tuple[str, str]:
    """Render a WMO code as a (label, emoji) pair, tolerating unknown codes."""
    if code is None:
        return ("Unknown", "🌡️")
    return WEATHER_CODES.get(int(code), ("Unsettled", "🌡️"))


@dataclass
class CurrentWeather:
    """Conditions at the moment of the request."""

    temperature: float | None = None
    apparent_temperature: float | None = None
    humidity: float | None = None
    wind_speed: float | None = None
    code: int | None = None
    observed_at: str | None = None


@dataclass
class DayOutlook:
    """The provider's summary for the whole of today."""

    high: float | None = None
    low: float | None = None
    code: int | None = None
    rain_chance: int | None = None
    precipitation: float | None = None
    uv_index: float | None = None
    sunrise: str | None = None
    sunset: str | None = None


@dataclass
class Weather:
    """Current conditions plus today's outlook, in the location's timezone."""

    current: CurrentWeather
    today: DayOutlook
    utc_offset_seconds: int | None = None

    def current_local_time(self) -> datetime | None:
        """The current moment where the weather is, never the host's clock.

        The briefing greets by time of day, and the host may well be in a
        different timezone from the place being watched. Reconstructing local
        time from UTC plus the reported offset keeps that correct without
        depending on how the container's `/etc/localtime` happens to be set.
        """
        if self.utc_offset_seconds is None:
            return None
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        return utc_now + timedelta(seconds=self.utc_offset_seconds)


def _first(values: object) -> object | None:
    """Return the first element of a daily array, or None when it is absent.

    Open-Meteo returns every `daily` field as a list even for a single day.
    """
    if isinstance(values, list) and values:
        return values[0]
    return None


def _as_float(value: object) -> float | None:
    """Coerce a provider value to float, returning None when it is unusable."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    """Coerce a provider value to int, returning None when it is unusable."""
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    return None


def fetch_weather(lat: float, lon: float) -> Weather | None:
    """Fetch current conditions and today's outlook, or None if unavailable."""
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum,uv_index_max,sunrise,sunset"
            ),
            "timezone": "auto",
            "forecast_days": 1,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{query}"
    try:
        payload = http_get_json(url)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("Weather request failed: %s", exc)
        return None

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    offset = payload.get("utc_offset_seconds")

    return Weather(
        current=CurrentWeather(
            temperature=_as_float(current.get("temperature_2m")),
            apparent_temperature=_as_float(current.get("apparent_temperature")),
            humidity=_as_float(current.get("relative_humidity_2m")),
            wind_speed=_as_float(current.get("wind_speed_10m")),
            code=_as_int(current.get("weather_code")),
            observed_at=current.get("time"),
        ),
        today=DayOutlook(
            high=_as_float(_first(daily.get("temperature_2m_max"))),
            low=_as_float(_first(daily.get("temperature_2m_min"))),
            code=_as_int(_first(daily.get("weather_code"))),
            rain_chance=_as_int(_first(daily.get("precipitation_probability_max"))),
            precipitation=_as_float(_first(daily.get("precipitation_sum"))),
            uv_index=_as_float(_first(daily.get("uv_index_max"))),
            sunrise=_first(daily.get("sunrise")),
            sunset=_first(daily.get("sunset")),
        ),
        utc_offset_seconds=int(offset) if isinstance(offset, (int, float)) else None,
    )
