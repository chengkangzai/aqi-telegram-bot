#!/usr/bin/env python3
"""Render the AQI forecast as a PNG for delivery over Telegram.

Design notes, since they are easy to undo by accident:

* One series, so there is no legend box - the title names it.
* The line is neutral ink, not a severity colour. Severity is carried by the
  banded background plus its text labels, so the reading is never colour-alone.
* The bands are AQI's standard "semantic heat" scale, kept at low alpha so they
  stay recessive context rather than saturated blocks competing with the line.
* Only the peak is direct-labelled. A number on every point is noise.
* Light and dark are separately chosen palettes, not an inversion of each other.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from aqi_bot import BANDS, ForecastPoint, band_index  # noqa: E402

# Standard AQI category colours. Used only as low-alpha background bands.
BAND_COLOURS = ["#00b050", "#e8c400", "#ef8b34", "#e04a4a", "#8f5fa8", "#7e3a4e"]

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "band_alpha": 0.16,
        "band_label_alpha": 0.85,
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "band_alpha": 0.15,
        "band_label_alpha": 0.95,
    },
}


def _parse(stamp: str) -> datetime | None:
    """Parse an ISO local timestamp, tolerating junk."""
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


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
    values = [aqi for _, aqi in samples]
    peak_index = max(range(len(values)), key=lambda i: values[i])

    figure, axes = plt.subplots(figsize=(6.4, 3.9), dpi=170)
    figure.patch.set_facecolor(palette["surface"])
    axes.set_facecolor(palette["surface"])

    # Anchor the view to one full band below the data rather than always to
    # zero: on a line chart the bands already carry absolute severity, and a
    # forced zero baseline squeezes all the real variation into the top third.
    lowest, highest = min(values), max(values)
    low_band = band_index(lowest)
    bottom = BANDS[low_band][0] if low_band > 0 else 0
    top = highest + max(20, (highest - bottom) * 0.30)
    span = top - bottom

    # --- severity bands (the scale legend for the semantic-heat colours) ---
    for index, (lower, upper, label, _emoji) in enumerate(BANDS):
        if lower >= top or upper <= bottom:
            continue
        visible_low, visible_high = max(lower, bottom), min(upper, top)
        axes.axhspan(
            visible_low,
            visible_high,
            color=BAND_COLOURS[index],
            alpha=palette["band_alpha"],
            linewidth=0,
            zorder=0,
        )
        # Only label a band with room for the text to sit inside it.
        if visible_high - visible_low > span * 0.09:
            axes.text(
                0.995,
                (visible_low + visible_high) / 2,
                label.replace("Unhealthy for Sensitive Groups", "Sensitive groups"),
                transform=axes.get_yaxis_transform(),
                ha="right",
                va="center",
                fontsize=7.5,
                color=palette["secondary"],
                alpha=palette["band_label_alpha"],
                zorder=1,
            )

    # --- the series ---
    axes.plot(times, values, linewidth=2, color=palette["ink"], zorder=3, solid_capstyle="round")

    # --- selective direct label: the peak only ---
    axes.plot(
        [times[peak_index]],
        [values[peak_index]],
        marker="o",
        markersize=7,
        color=palette["ink"],
        markeredgecolor=palette["surface"],
        markeredgewidth=2,
        zorder=4,
    )
    peak_band = band_index(values[peak_index])
    # A peak near either edge would push a centred label off the canvas, so
    # anchor the label inward whenever it sits in the outer fifth of the plot.
    position = peak_index / max(1, len(values) - 1)
    if position < 0.2:
        peak_align, peak_offset = "left", -6
    elif position > 0.8:
        peak_align, peak_offset = "right", 6
    else:
        peak_align, peak_offset = "center", 0
    axes.annotate(
        f"peak {values[peak_index]} · {BANDS[peak_band][2].replace('Unhealthy for Sensitive Groups', 'Sensitive groups')}",
        xy=(times[peak_index], values[peak_index]),
        xytext=(peak_offset, 11),
        textcoords="offset points",
        ha=peak_align,
        fontsize=9,
        fontweight="bold",
        color=palette["ink"],
        zorder=5,
    )

    # --- "now" marker ---
    # Solid, not dashed: a dashed rule reads as a projection or threshold, and
    # this is a statement of fact about where the present sits.
    if now is not None and times[0] <= now <= times[-1]:
        axes.axvline(
            now,
            color=palette["secondary"],
            linewidth=1.2,
            zorder=2,
        )
        # Anchor the label inward when the line is near an edge.
        elapsed = (now - times[0]).total_seconds()
        total = max(1.0, (times[-1] - times[0]).total_seconds())
        near_end = elapsed / total > 0.85
        axes.annotate(
            "now",
            xy=(now, top),
            xytext=(-5 if near_end else 5, -4),
            textcoords="offset points",
            ha="right" if near_end else "left",
            va="top",
            fontsize=8,
            fontweight="bold",
            color=palette["secondary"],
            zorder=5,
        )

    # --- chrome, kept recessive ---
    axes.set_ylim(bottom, top)
    # Extend past the last sample so the band labels sit in clear space rather
    # than on top of the series.
    gutter = (times[-1] - times[0]) * 0.16
    axes.set_xlim(times[0], times[-1] + gutter)
    axes.grid(axis="y", color=palette["grid"], linewidth=0.8, zorder=1)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(palette["axis"])
    axes.spines["bottom"].set_linewidth(0.8)
    axes.tick_params(colors=palette["muted"], labelsize=8, length=0)

    # Build ticks by hand rather than with a locator: the x-limit runs past the
    # last sample to make room for the band labels, and a locator would happily
    # place a tick out in that empty gutter.
    span_hours = (times[-1] - times[0]).total_seconds() / 3600
    step = 3 if span_hours <= 26 else (6 if span_hours <= 52 else 12)
    ticks = []
    cursor = times[0].replace(minute=0, second=0, microsecond=0)
    while cursor <= times[-1]:
        if cursor >= times[0] and cursor.hour % step == 0:
            ticks.append(cursor)
        cursor += timedelta(hours=1)
    axes.set_xticks(ticks)
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%H:%M"))
    for label in axes.get_yticklabels() + axes.get_xticklabels():
        label.set_color(palette["muted"])

    axes.set_title(
        f"Air quality forecast — {location}",
        loc="left",
        fontsize=12,
        fontweight="bold",
        color=palette["ink"],
        pad=16,
    )
    axes.text(
        0,
        1.035,
        f"Next {len(values)} hours · US AQI · {source}",
        transform=axes.transAxes,
        fontsize=8.5,
        color=palette["muted"],
    )

    figure.tight_layout(pad=1.1)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=palette["surface"], bbox_inches="tight", pad_inches=0.22)
    plt.close(figure)
    return buffer.getvalue()
