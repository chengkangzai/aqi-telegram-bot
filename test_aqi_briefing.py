"""Unit tests for the morning briefing. Run: python3 -m unittest -v"""

import unittest
from datetime import datetime

from aqi_bot import ForecastPoint, Reading
from aqi_briefing import (
    collect_advice,
    compose_briefing,
    format_clock,
    greeting_for,
    points_for_today,
)
from aqi_weather import CurrentWeather, DayOutlook, Weather, describe_code


def make_weather(**overrides):
    """Build a plausible tropical morning, overriding whichever field matters."""
    current = CurrentWeather(
        temperature=overrides.get("temperature", 26.4),
        apparent_temperature=overrides.get("apparent_temperature", 29.1),
        humidity=overrides.get("humidity", 82.0),
        wind_speed=overrides.get("wind_speed", 8.0),
        code=overrides.get("code", 2),
        observed_at="2026-09-09T10:00",
    )
    today = DayOutlook(
        high=overrides.get("high", 32.0),
        low=overrides.get("low", 24.0),
        code=overrides.get("day_code", 95),
        rain_chance=overrides.get("rain_chance", 20),
        precipitation=overrides.get("precipitation", 0.0),
        uv_index=overrides.get("uv_index", 6.0),
        sunrise="2026-09-09T07:05",
        sunset="2026-09-09T19:12",
    )
    return Weather(current=current, today=today, utc_offset_seconds=28800)


class DescribeCodeTests(unittest.TestCase):
    def test_known_code_is_named(self):
        label, emoji = describe_code(95)
        self.assertEqual(label, "Thunderstorm")
        self.assertTrue(emoji)

    def test_unknown_code_does_not_crash(self):
        self.assertEqual(describe_code(1234)[0], "Unsettled")

    def test_missing_code_does_not_crash(self):
        self.assertEqual(describe_code(None)[0], "Unknown")


class GreetingTests(unittest.TestCase):
    def test_greeting_tracks_the_local_hour(self):
        cases = [(7, "Good morning"), (13, "Good afternoon"), (21, "Good evening")]
        for hour, expected in cases:
            with self.subTest(hour=hour):
                moment = datetime(2026, 9, 9, hour, 0)
                self.assertEqual(greeting_for(moment), expected)

    def test_unknown_time_assumes_the_scheduled_slot(self):
        self.assertEqual(greeting_for(None), "Good morning")


class FormatClockTests(unittest.TestCase):
    def test_iso_stamp_becomes_a_bare_clock_time(self):
        self.assertEqual(format_clock("2026-09-09T07:05"), "07:05")

    def test_unusable_values_return_none(self):
        self.assertIsNone(format_clock("not a time"))
        self.assertIsNone(format_clock(None))


class PointsForTodayTests(unittest.TestCase):
    def test_only_todays_hours_are_kept(self):
        points = [ForecastPoint(time=f"2026-09-09T{hour:02d}:00", aqi=60) for hour in range(10, 24)]
        points += [ForecastPoint(time=f"2026-09-10T{hour:02d}:00", aqi=90) for hour in range(0, 10)]
        kept, window = points_for_today(points, datetime(2026, 9, 9, 10, 0))
        self.assertEqual(window, "today")
        self.assertTrue(all(point.time.startswith("2026-09-09") for point in kept))
        self.assertEqual(len(kept), 14)

    def test_late_evening_rolls_the_window_forward(self):
        # Two hours left of today is not a day's outlook; look ahead instead.
        points = [ForecastPoint(time="2026-09-09T22:00", aqi=60), ForecastPoint(time="2026-09-09T23:00", aqi=62)]
        points += [ForecastPoint(time=f"2026-09-10T{hour:02d}:00", aqi=90) for hour in range(0, 10)]
        kept, window = points_for_today(points, datetime(2026, 9, 9, 22, 0))
        self.assertEqual(window, "the next 12 hours")
        self.assertEqual(len(kept), 12)

    def test_empty_series_is_handled(self):
        self.assertEqual(points_for_today([], datetime(2026, 9, 9, 10, 0)), ([], "today"))


class AdviceTests(unittest.TestCase):
    def test_a_wet_day_earns_an_umbrella(self):
        advice = collect_advice(make_weather(rain_chance=80), None, [])
        self.assertIn("Take an umbrella.", advice)

    def test_a_dry_calm_clean_day_says_so(self):
        reading = Reading(aqi=30, source="WAQI ground station")
        advice = collect_advice(make_weather(), reading, [])
        self.assertEqual(advice, ["Nothing to plan around. Ordinary day."])

    def test_harsh_uv_is_called_out(self):
        advice = collect_advice(make_weather(uv_index=11.0), None, [])
        self.assertTrue(any("UV" in line for line in advice))

    def test_advice_follows_the_forecast_peak_not_just_the_reading(self):
        # Clean right now, but the afternoon is not. The morning is when that
        # is worth knowing.
        reading = Reading(aqi=40, source="WAQI ground station")
        points = [ForecastPoint(time="2026-09-09T15:00", aqi=165)]
        advice = collect_advice(make_weather(), reading, points)
        self.assertTrue(any("mask" in line.lower() for line in advice))


class ComposeBriefingTests(unittest.TestCase):
    def test_a_full_briefing_carries_both_halves(self):
        reading = Reading(aqi=62, source="WAQI ground station", station="Cheras")
        points = [ForecastPoint(time=f"2026-09-09T{hour:02d}:00", aqi=aqi)
                  for hour, aqi in ((10, 62), (15, 118), (20, 70))]
        text = compose_briefing(make_weather(), reading, points, "Cheras", datetime(2026, 9, 9, 10, 0))

        self.assertIn("Good morning", text)
        self.assertIn("Cheras", text)
        self.assertIn("26.4°C", text)
        self.assertIn("24°C to 32°C", text)
        self.assertIn("07:05", text)
        self.assertIn("AQI 62", text)
        self.assertIn("Peak today", text)
        self.assertIn("AQI 118", text)

    def test_briefing_survives_the_weather_being_down(self):
        reading = Reading(aqi=62, source="Open-Meteo (modelled)")
        text = compose_briefing(None, reading, [], "Cheras", None)
        self.assertIn("Weather is unavailable", text)
        self.assertIn("AQI 62", text)

    def test_briefing_survives_the_air_reading_being_down(self):
        text = compose_briefing(make_weather(), None, [], "Cheras", datetime(2026, 9, 9, 10, 0))
        self.assertIn("no reading available", text)
        self.assertIn("26.4°C", text)

    def test_location_is_html_escaped(self):
        text = compose_briefing(make_weather(), None, [], "Home & <Garden>", datetime(2026, 9, 9, 10, 0))
        self.assertIn("Home &amp; &lt;Garden&gt;", text)
        self.assertNotIn("<Garden>", text)


if __name__ == "__main__":
    unittest.main()
