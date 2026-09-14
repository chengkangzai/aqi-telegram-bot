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

The time axis is read the way the summary sentence is written. A multi-day
window of bare hours ("06h 12h 18h 00h 06h ...") gave no way to find "Wed
19:00" on the chart, so midnight is drawn as a divider and labelled with the
day it opens, and the peak carries its own day and time.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
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
        "axis": "#4a4a46",
        "band_alpha": 0.15,
    },
}

# IBM Plex from Debian's fonts-ibm-plex. The condensed cut sets numbers and
# axis labels like an instrument readout and fits more ticks across a phone
# screen. Registered by path so the service account needs no font cache.
PLEX_DIR = Path("/usr/share/fonts/truetype/ibm-plex")
FALLBACK_FAMILY = "DejaVu Sans"


def _load_fonts() -> tuple[str, str]:
    """Return (text family, condensed family), falling back to DejaVu."""
    if not PLEX_DIR.is_dir():
        return FALLBACK_FAMILY, FALLBACK_FAMILY
    for path in PLEX_DIR.glob("IBMPlexSans*.ttf"):
        try:
            font_manager.fontManager.addfont(str(path))
        except (OSError, RuntimeError):
            continue
    names = {entry.name for entry in font_manager.fontManager.ttflist}
    text = "IBM Plex Sans" if "IBM Plex Sans" in names else FALLBACK_FAMILY
    condensed = "IBM Plex Sans Condensed" if "IBM Plex Sans Condensed" in names else text
    return text, condensed


TEXT_FONT, CONDENSED_FONT = _load_fonts()


def _parse(stamp: str) -> datetime | None:
    """Parse an ISO local timestamp, tolerating junk."""
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _short(label: str) -> str:
    """Shorten a band name for tight label space."""
    return SHORT_NAMES.get(label, label)


def _clock_after(target: datetime, anchor: datetime) -> str:
    """A clock time for `target`, naming the day only if it differs from `anchor`."""
    if target.date() == anchor.date():
        return f"{target:%H:%M}"
    return f"{target:%a %H:%M}"


def _summary_sentence(
    values: np.ndarray,
    times: list[datetime],
    peak_index: int,
    now: datetime | None,
) -> str:
    """Describe the window in one plain-language sentence.

    The trough is taken from *after* the peak: the global minimum is often the
    first sample, which would produce "then eases to 30 right about now" about
    a value the reader is already looking at. It is timed against the peak
    rather than against now, so the second clause does not repeat "in about
    2 days" for something three hours after the first.
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
        f"then eases to {values[low]} by {_clock_after(times[low], times[peak_index])}."
    )


def _tick_step(span_hours: float) -> int:
    """Hours between hour labels, keeping roughly 8-16 labels across the plot."""
    if span_hours <= 12:
        return 2
    if span_hours <= 26:
        return 3
    if span_hours <= 60:
        return 6
    return 12


def _time_labels(
    start: datetime, end: datetime, now: datetime | None
) -> tuple[list[datetime], list[tuple[datetime, str, str]]]:
    """Plan the x axis: midnight dividers plus (time, text, kind) labels.

    Kinds rank "day" > "now" > "hour". A lower-ranked label that would crowd a
    higher-ranked one is dropped rather than overlapped, so a 05:20 "now" hides
    the "06" beside it and a 23:40 "now" gives way to the next day's name.
    """
    span_hours = (end - start).total_seconds() / 3600
    step = _tick_step(span_hours)
    min_gap = timedelta(hours=max(1.0, span_hours * 0.055))

    midnights: list[datetime] = []
    labels: list[tuple[datetime, str, str]] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(hours=1)
    while cursor <= end:
        if cursor.hour == 0:
            # A divider on the very first sample marks nothing.
            if cursor > start:
                midnights.append(cursor)
            labels.append((cursor, f"{cursor:%a} {cursor.day}", "day"))
        elif cursor.hour % step == 0:
            labels.append((cursor, f"{cursor:%H}", "hour"))
        cursor += timedelta(hours=1)

    if now is not None and start <= now <= end:
        labels.append((now, "now", "now"))

    rank = {"day": 0, "now": 1, "hour": 2}
    kept: list[tuple[datetime, str, str]] = []
    for label in sorted(labels, key=lambda item: rank[item[2]]):
        if all(abs(label[0] - other[0]) >= min_gap for other in kept):
            kept.append(label)
    return midnights, sorted(kept)


def _place_headline(figure, palette: dict, location: str, current: int) -> None:
    """Draw the location, the current reading, and its band beside it."""
    band = band_index(current)
    renderer = figure.canvas.get_renderer()
    to_figure = figure.transFigure.inverted()

    figure.text(
        0.10, 0.965, location.upper(), fontsize=8.5, family=TEXT_FONT,
        color=palette["muted"], fontweight="semibold", va="top",
    )
    number = figure.text(
        0.097, 0.918, str(current), fontsize=46, family=CONDENSED_FONT,
        fontweight="semibold", color=BAND_COLOURS[band], va="top", ha="left",
    )
    extent = number.get_window_extent(renderer).transformed(to_figure)
    beside = extent.x1 + 0.022
    middle = (extent.y0 + extent.y1) / 2

    figure.text(
        beside, middle + 0.006, BANDS[band][2].upper(), fontsize=12,
        family=TEXT_FONT, fontweight="bold", color=palette["ink"], va="bottom",
    )
    figure.text(
        beside, middle - 0.004, "right now · US AQI", fontsize=9,
        family=TEXT_FONT, color=palette["muted"], va="top",
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

    # Tick labels are rebuilt at draw time, so the condensed face has to be the
    # default rather than set on the labels afterwards.
    with plt.rc_context({"font.family": CONDENSED_FONT}):
        return _render(samples, location, THEMES.get(theme, THEMES["light"]), source, now)


def _render(
    samples: list[tuple[datetime, int]],
    location: str,
    palette: dict,
    source: str,
    now: datetime | None,
) -> bytes:
    """Draw the figure for already-parsed samples."""
    times = [when for when, _ in samples]
    values = np.array([aqi for _, aqi in samples])
    peak_index = int(values.argmax())
    current = int(values[0])

    figure = plt.figure(figsize=(6.6, 4.7), dpi=170)
    figure.patch.set_facecolor(palette["surface"])
    axes = figure.add_axes([0.10, 0.13, 0.71, 0.53])
    axes.set_facecolor(palette["surface"])

    # --- headline ---
    _place_headline(figure, palette, location, current)
    figure.text(
        0.10, 0.772,
        _summary_sentence(values, times, peak_index, now),
        fontsize=10, family=TEXT_FONT, color=palette["secondary"],
        va="top", linespacing=1.5,
    )

    # --- the plot: anchored one band below the data, not at zero ---
    lowest, highest = int(values.min()), int(values.max())
    low_band = band_index(lowest)
    bottom = BANDS[low_band - 1][1] if low_band > 0 else 0
    top = highest + max(15, (highest - bottom) * 0.22)
    span = top - bottom

    for index, (_lower, upper, label, _emoji) in enumerate(BANDS):
        # Bands meet at the previous band's upper bound (150, 200, ...) so the
        # integer gap between 200 and 201 does not paint a stripe of surface.
        lower = BANDS[index - 1][1] if index > 0 else 0
        if lower >= top or upper <= bottom:
            continue
        visible_low, visible_high = max(lower, bottom), min(upper, top)
        axes.axhspan(
            visible_low, visible_high, color=BAND_COLOURS[index],
            alpha=palette["band_alpha"], linewidth=0, zorder=0,
        )
        if visible_high - visible_low > span * 0.09:
            axes.text(
                1.022, (visible_low + visible_high) / 2, _short(label),
                transform=axes.get_yaxis_transform(), ha="left", va="center",
                fontsize=8, family=CONDENSED_FONT, color=palette["secondary"],
                zorder=1,
            )

    # --- time axis: days as dividers, hours as quiet labels ---
    midnights, labels = _time_labels(times[0], times[-1], now)
    for midnight in midnights:
        axes.axvline(midnight, color=palette["axis"], linewidth=0.9, zorder=1)

    edge = (times[-1] - times[0]) * 0.02
    for when, text, kind in labels:
        if when <= times[0] + edge:
            align = "left"
        elif when >= times[-1] - edge:
            align = "right"
        else:
            align = "center"
        axes.text(
            when, -0.035, text, transform=axes.get_xaxis_transform(),
            ha=align, va="top",
            fontsize=8.5 if kind != "hour" else 8, family=CONDENSED_FONT,
            fontweight="semibold" if kind != "hour" else "normal",
            color=palette["ink"] if kind == "day" else (
                palette["secondary"] if kind == "now" else palette["muted"]
            ),
        )

    # --- "now" marker: solid, because it states a fact rather than a forecast.
    # Skipped when it would sit on the left edge and merely thicken the frame.
    if now is not None and times[0] <= now <= times[-1]:
        elapsed = (now - times[0]).total_seconds()
        total = max(1.0, (times[-1] - times[0]).total_seconds())
        if elapsed / total > 0.02:
            axes.axvline(now, color=palette["secondary"], linewidth=1.1, zorder=2)

    axes.plot(
        times, values, linewidth=2.1, color=palette["ink"],
        zorder=3, solid_capstyle="round", solid_joinstyle="round",
    )

    # --- peak: marker plus its own value and time, so the summary sentence can
    # be checked against the chart without counting ticks.
    peak_time, peak_value = times[peak_index], int(values[peak_index])
    axes.plot(
        [peak_time], [peak_value], marker="o", markersize=6.5,
        color=palette["ink"], markeredgecolor=palette["surface"],
        markeredgewidth=2, zorder=4, clip_on=False,
    )
    if peak_index > 0:
        position = peak_index / (len(times) - 1)
        align = "right" if position > 0.85 else ("left" if position < 0.15 else "center")
        nudge = {"right": -2, "left": 2, "center": 0}[align]
        value_style = {
            "fontsize": 9.5, "family": CONDENSED_FONT, "fontweight": "bold",
            "color": palette["ink"], "va": "baseline", "zorder": 5,
        }
        time_style = {
            "fontsize": 8.5, "family": CONDENSED_FONT, "color": palette["muted"],
            "va": "baseline", "zorder": 5,
        }
        value_text = f"{peak_value}"
        time_text = f"  {_clock_after(peak_time, now or times[0])}"
        # Always read "205  Wed 19:00". Near the right edge the pair is built
        # leftwards from the time, so the order survives right alignment.
        if align == "right":
            anchor = axes.annotate(
                time_text, xy=(peak_time, peak_value), xytext=(nudge, 11),
                textcoords="offset points", ha="right", **time_style,
            )
            axes.annotate(
                value_text, xycoords=anchor, xy=(0, 0), ha="right", **value_style,
            )
        else:
            anchor = axes.annotate(
                value_text, xy=(peak_time, peak_value), xytext=(nudge, 11),
                textcoords="offset points", ha=align, **value_style,
            )
            axes.annotate(
                time_text, xycoords=anchor, xy=(1, 0), ha="left", **time_style,
            )

    axes.set_ylim(bottom, top)
    axes.set_xlim(times[0], times[-1])
    axes.set_xticks([])
    axes.yaxis.set_major_locator(mticker.MaxNLocator(nbins=5, integer=True, steps=[1, 2, 5, 10]))
    axes.grid(axis="y", color=palette["grid"], linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(palette["axis"])
    axes.spines["bottom"].set_linewidth(0.9)
    axes.tick_params(axis="y", colors=palette["muted"], labelsize=8, length=0, pad=6)

    figure.text(
        0.965, 0.035, f"US AQI · {source}", fontsize=7.5, family=TEXT_FONT,
        color=palette["muted"], ha="right",
    )

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=palette["surface"])
    plt.close(figure)
    return buffer.getvalue()
