#!/usr/bin/env python3
"""Render the AQI forecast as a PNG for delivery over Telegram.

The layout splits two questions that were fighting each other:

* The **headline** owns "how bad is it" — the current reading, large, in its
  band colour, with the band name beside it so severity is never colour-alone,
  plus a plain-language sentence about where it goes next.
* The **chart** owns "how does it change" — a zoomed axis over recessive
  severity bands. It does not have to shout, because the headline already did.

Earlier attempts collapsed both jobs into the plot and failed either way: a
severity-coloured fill on a truncated axis reads as permanently maxed out, and
anchoring that fill to zero spends most of the canvas on constant colour while
squashing the actual signal into the top quarter.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from aqi_bot import BANDS, ForecastPoint, band_index, relative_phrase  # noqa: E402

# Standard AQI category colours (semantic heat). Used as low-alpha background
# bands, always alongside their text label.
BAND_COLOURS = ["#00b050", "#e8c400", "#ef8b34", "#e04a4a", "#8f5fa8", "#7e3a4e"]

# Long band names do not fit the right-hand label margin.
SHORT_NAMES = {"Unhealthy for Sensitive Groups": "Sensitive groups"}

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "band_alpha": 0.16,
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "band_alpha": 0.15,
    },
}


def _parse(stamp: str) -> datetime | None:
    """Parse an ISO local timestamp, tolerating junk."""
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _short(label: str) -> str:
    """Shorten a band name for tight label space."""
    return SHORT_NAMES.get(label, label)


def _summary_sentence(
    values: np.ndarray,
    times: list[datetime],
    peak_index: int,
    now: datetime | None,
) -> str:
    """Describe the window in one plain-language sentence.

    The trough is taken from *after* the peak: the global minimum is often the
    first sample, which would produce "then eases to 30 right about now" about
    a value the reader is already looking at.
    """
    spread = int(values.max()) - int(values.min())
    if spread <= 4:
        return f"Holding steady around {int(values[0])} through the window."

    if peak_index == 0:
        low = int(values.argmin())
        return f"Easing from here — down to {values[low]} {relative_phrase(times[low], now)}."

    when_peak = relative_phrase(times[peak_index], now)
    # "Worsens" is only honest if the peak crosses into a worse band. A 22 to
    # 41 rise is still Good and should not read like a warning.
    crosses = band_index(int(values[peak_index])) > band_index(int(values[0]))
    verb = "Worsens to" if crosses else "Rises to"

    low = peak_index + int(values[peak_index:].argmin())
    if low == peak_index:
        return f"{verb} {values[peak_index]} {when_peak},\nstill climbing at the end of the window."
    return (
        f"{verb} {values[peak_index]} {when_peak},\n"
        f"then eases to {values[low]} {relative_phrase(times[low], now)}."
    )


def render_forecast_chart(
    points: list[ForecastPoint],
    location: str,
    theme: str = "light",
    source: str = "Open-Meteo (modelled)",
    now: datetime | None = None,
) -> bytes | None:
    """Render the forecast to PNG bytes, or None if there is nothing to draw."""
    samples = [(_parse(point.time), point.aqi) for point in points]
    samples = [(when, aqi) for when, aqi in samples if when is not None]
    if len(samples) < 2:
        return None

    palette = THEMES.get(theme, THEMES["light"])
    times = [when for when, _ in samples]
    values = np.array([aqi for _, aqi in samples])
    peak_index = int(values.argmax())
    current = int(values[0])
    current_band = band_index(current)

    figure = plt.figure(figsize=(6.6, 4.7), dpi=170)
    figure.patch.set_facecolor(palette["surface"])
    axes = figure.add_axes([0.10, 0.145, 0.71, 0.46])
    axes.set_facecolor(palette["surface"])

    # --- headline ---
    figure.text(
        0.10, 0.965, location.upper(), fontsize=8.5,
        color=palette["muted"], fontweight="bold", va="top",
    )
    figure.text(
        0.10, 0.925, str(current), fontsize=40, fontweight="bold",
        color=BAND_COLOURS[current_band], va="top", ha="left",
    )
    figure.text(
        0.10, 0.783, f"{BANDS[current_band][2].upper()}  ·  RIGHT NOW",
        fontsize=10.5, fontweight="bold", color=palette["ink"], va="top",
    )
    figure.text(
        0.10, 0.727,
        _summary_sentence(values, times, peak_index, now),
        fontsize=10, color=palette["secondary"], va="top", linespacing=1.45,
    )

    # --- the plot: anchored one band below the data, not at zero ---
    lowest, highest = int(values.min()), int(values.max())
    low_band = band_index(lowest)
    bottom = BANDS[low_band][0] if low_band > 0 else 0
    top = highest + max(15, (highest - bottom) * 0.22)
    span = top - bottom

    for index, (lower, upper, label, _emoji) in enumerate(BANDS):
        if lower >= top or upper <= bottom:
            continue
        visible_low, visible_high = max(lower, bottom), min(upper, top)
        axes.axhspan(
            visible_low, visible_high, color=BAND_COLOURS[index],
            alpha=palette["band_alpha"], linewidth=0, zorder=0,
        )
        if visible_high - visible_low > span * 0.09:
            axes.text(
                1.008, (visible_low + visible_high) / 2, _short(label),
                transform=axes.get_yaxis_transform(), ha="left", va="center",
                fontsize=7.5, color=palette["secondary"], zorder=1,
            )

    axes.plot(
        times, values, linewidth=2, color=palette["ink"],
        zorder=3, solid_capstyle="round",
    )
    # Peak marker only — the headline sentence already gives the number.
    axes.plot(
        [times[peak_index]], [values[peak_index]], marker="o", markersize=6,
        color=palette["ink"], markeredgecolor=palette["surface"],
        markeredgewidth=2, zorder=4,
    )

    # --- "now" marker: solid, because it states a fact rather than a forecast ---
    if now is not None and times[0] <= now <= times[-1]:
        axes.axvline(now, color=palette["secondary"], linewidth=1.2, zorder=2)
        elapsed = (now - times[0]).total_seconds()
        total = max(1.0, (times[-1] - times[0]).total_seconds())
        near_end = elapsed / total > 0.85
        axes.annotate(
            "now", xy=(now, top), xytext=(-5 if near_end else 5, -3),
            textcoords="offset points", ha="right" if near_end else "left",
            va="top", fontsize=8, fontweight="bold",
            color=palette["secondary"], zorder=5,
        )

    axes.set_ylim(bottom, top)
    axes.set_xlim(times[0], times[-1])
    axes.grid(axis="y", color=palette["grid"], linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(palette["axis"])
    axes.spines["bottom"].set_linewidth(0.8)
    axes.tick_params(colors=palette["muted"], labelsize=8, length=0)

    # Ticks built by hand so none can land outside the sampled range.
    span_hours = (times[-1] - times[0]).total_seconds() / 3600
    step = 3 if span_hours <= 26 else (6 if span_hours <= 52 else 12)
    ticks: list[datetime] = []
    cursor = times[0].replace(minute=0, second=0, microsecond=0)
    while cursor <= times[-1]:
        if cursor >= times[0] and cursor.hour % step == 0:
            ticks.append(cursor)
        cursor += timedelta(hours=1)
    axes.set_xticks(ticks)
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%Hh"))

    figure.text(
        0.965, 0.045, f"US AQI · {source}", fontsize=7.5,
        color=palette["muted"], ha="right",
    )

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=palette["surface"])
    plt.close(figure)
    return buffer.getvalue()
