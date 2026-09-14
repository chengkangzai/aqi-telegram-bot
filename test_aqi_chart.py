"""Unit tests for the forecast chart. Run: python3 -m unittest -v"""

import unittest
from datetime import datetime, timedelta

import numpy as np

try:
    import aqi_chart
except ImportError:  # matplotlib is optional; the listener falls back to text
    aqi_chart = None

from aqi_bot import ForecastPoint


def hourly(start, values):
    """Build hourly forecast points from a start time and a list of AQI values."""
    return [
        ForecastPoint(time=(start + timedelta(hours=i)).isoformat(timespec="minutes"), aqi=v)
        for i, v in enumerate(values)
    ]


@unittest.skipIf(aqi_chart is None, "matplotlib not installed")
class TimeLabelTests(unittest.TestCase):
    def test_midnight_is_labelled_with_the_day_it_opens(self):
        start = datetime(2026, 9, 15, 1, 0)
        midnights, labels = aqi_chart._time_labels(start, start + timedelta(hours=47), None)
        self.assertEqual(midnights, [datetime(2026, 9, 16, 0, 0), datetime(2026, 9, 17, 0, 0)])
        days = [text for _when, text, kind in labels if kind == "day"]
        self.assertEqual(days, ["Wed 16", "Thu 17"])
        self.assertNotIn("00", [text for _when, text, _kind in labels])

    def test_now_hides_an_hour_label_it_would_crowd(self):
        start = datetime(2026, 9, 15, 5, 0)
        now = datetime(2026, 9, 15, 5, 40)
        _midnights, labels = aqi_chart._time_labels(start, start + timedelta(hours=24), now)
        texts = [text for _when, text, _kind in labels]
        self.assertEqual(texts[0], "now")
        self.assertNotIn("06", texts)

    def test_day_name_wins_over_a_crowding_now(self):
        start = datetime(2026, 9, 15, 23, 0)
        now = datetime(2026, 9, 15, 23, 40)
        _midnights, labels = aqi_chart._time_labels(start, start + timedelta(hours=24), now)
        texts = [text for _when, text, _kind in labels]
        self.assertIn("Wed 16", texts)
        self.assertNotIn("now", texts)

    def test_no_now_label_without_a_known_now(self):
        start = datetime(2026, 9, 15, 9, 0)
        _midnights, labels = aqi_chart._time_labels(start, start + timedelta(hours=12), None)
        self.assertNotIn("now", [text for _when, text, _kind in labels])


@unittest.skipIf(aqi_chart is None, "matplotlib not installed")
class SummarySentenceTests(unittest.TestCase):
    def test_trough_is_timed_against_the_peak_not_now(self):
        start = datetime(2026, 9, 15, 1, 0)
        values = np.array([172] + [170] * 40 + [205, 180, 154, 155])
        times = [start + timedelta(hours=i) for i in range(len(values))]
        sentence = aqi_chart._summary_sentence(values, times, 41, start)
        self.assertIn("(Wed 18:00)", sentence)
        self.assertTrue(sentence.endswith("then eases to 154 by 20:00."))
        self.assertEqual(sentence.count("days"), 1)

    def test_trough_on_a_later_day_names_the_day(self):
        start = datetime(2026, 9, 15, 16, 0)
        values = np.array([60, 90, 120, 100, 80, 70, 55])
        times = [start + timedelta(hours=2 * i) for i in range(len(values))]
        sentence = aqi_chart._summary_sentence(values, times, 2, start)
        self.assertTrue(sentence.endswith("by Wed 04:00."))


@unittest.skipIf(aqi_chart is None, "matplotlib not installed")
class RenderTests(unittest.TestCase):
    def test_renders_png_across_window_lengths_and_themes(self):
        start = datetime(2026, 9, 15, 9, 0)
        for hours in (6, 24, 48, 96):
            for theme in ("light", "dark"):
                with self.subTest(hours=hours, theme=theme):
                    values = [int(90 + 40 * np.sin(i / 7)) for i in range(hours + 1)]
                    png = aqi_chart.render_forecast_chart(
                        hourly(start, values), "Bukit Jalil, KL", theme,
                        now=start + timedelta(minutes=20),
                    )
                    self.assertTrue(png.startswith(b"\x89PNG"))

    def test_nothing_to_draw_returns_none(self):
        self.assertIsNone(
            aqi_chart.render_forecast_chart(hourly(datetime(2026, 9, 15), [80]), "KL")
        )


if __name__ == "__main__":
    unittest.main()
