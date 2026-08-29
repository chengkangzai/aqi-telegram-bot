"""Unit tests for the listener's parsing, authorisation and rendering."""

import unittest
from unittest import mock

import aqi_listener
from aqi_bot import Reading


BASE_CONFIG = {
    "token": "t",
    "chat_id": "42",
    "allowed_users": {42},
    "lat": 3.0491,
    "lon": 101.6804,
    "location": "Bukit Jalil, KL",
    "waqi_token": None,
    "state_path": "/nonexistent/state.json",
    "offset_path": "/nonexistent/offset.json",
    "chart_theme": "light",
    "alert_floor": 2,
    "deadband": 3,
}


class ExtractCommandTests(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(aqi_listener.extract_command("/now"), "now")

    def test_command_with_arguments(self):
        self.assertEqual(aqi_listener.extract_command("/status extra words"), "status")

    def test_command_addressed_to_bot_in_group(self):
        self.assertEqual(aqi_listener.extract_command("/now@CCKAQIBot"), "now")

    def test_case_is_normalised(self):
        self.assertEqual(aqi_listener.extract_command("/NOW"), "now")

    def test_plain_text_is_not_a_command(self):
        self.assertIsNone(aqi_listener.extract_command("hello there"))

    def test_leading_whitespace_tolerated(self):
        self.assertEqual(aqi_listener.extract_command("   /help"), "help")


class AuthorisationTests(unittest.TestCase):
    def _update(self, user_id, text="/now"):
        return {
            "update_id": 1,
            "message": {"from": {"id": user_id}, "chat": {"id": user_id}, "text": text},
        }

    def test_stranger_gets_no_reply(self):
        with mock.patch.object(aqi_listener, "send_telegram") as sender:
            aqi_listener.process_update(self._update(999), BASE_CONFIG)
        sender.assert_not_called()

    def test_authorised_user_gets_a_reply(self):
        with mock.patch.object(aqi_listener, "send_telegram") as sender:
            aqi_listener.process_update(self._update(42, "/help"), BASE_CONFIG)
        sender.assert_called_once()

    def test_non_command_from_authorised_user_is_ignored(self):
        with mock.patch.object(aqi_listener, "send_telegram") as sender:
            aqi_listener.process_update(self._update(42, "good morning"), BASE_CONFIG)
        sender.assert_not_called()


class RenderingTests(unittest.TestCase):
    def test_describe_reading_includes_band_and_source(self):
        reading = Reading(aqi=170, source="Open-Meteo (modelled)", pm25=95.2)
        text = aqi_listener.describe_reading(reading, "Bukit Jalil, KL")
        self.assertIn("AQI 170", text)
        self.assertIn("Unhealthy", text)
        self.assertIn("Bukit Jalil", text)
        self.assertIn("Open-Meteo", text)

    def test_status_handles_a_missing_state_file(self):
        text = aqi_listener.describe_status(BASE_CONFIG)
        self.assertIn("No check has completed yet", text)
        self.assertIn("Bukit Jalil", text)

    def test_status_reports_open_meteo_when_no_waqi_token(self):
        self.assertIn("no WAQI token set", aqi_listener.describe_status(BASE_CONFIG))

    def test_help_lists_every_command(self):
        text = aqi_listener.describe_help()
        for name, _desc in aqi_listener.COMMANDS:
            self.assertIn(f"/{name}", text)

    def test_unknown_command_is_escaped(self):
        reply, _ = aqi_listener.handle_command("<script>", BASE_CONFIG)
        self.assertIn("&lt;script&gt;", reply)
        self.assertNotIn("<script>", reply)

    def test_now_reports_failure_gracefully(self):
        with mock.patch.object(aqi_listener, "get_reading", side_effect=RuntimeError("down")):
            reply, _ = aqi_listener.handle_command("now", BASE_CONFIG)
        self.assertIn("Could not reach", reply)


if __name__ == "__main__":
    unittest.main()


class ForecastTests(unittest.TestCase):
    def _points(self, values, start_hour=7):
        from aqi_bot import ForecastPoint
        return [
            ForecastPoint(time=f"2026-08-29T{(start_hour + i) % 24:02d}:00", aqi=v)
            for i, v in enumerate(values)
        ]

    def test_parse_hours_defaults_when_absent(self):
        self.assertEqual(aqi_listener.parse_forecast_hours([]), 24)

    def test_parse_hours_accepts_a_number(self):
        self.assertEqual(aqi_listener.parse_forecast_hours(["48"]), 48)

    def test_parse_hours_clamps_both_ends(self):
        self.assertEqual(aqi_listener.parse_forecast_hours(["1"]), 6)
        self.assertEqual(aqi_listener.parse_forecast_hours(["500"]), 96)

    def test_parse_hours_ignores_rubbish(self):
        self.assertEqual(aqi_listener.parse_forecast_hours(["soon"]), 24)

    def test_extract_args(self):
        self.assertEqual(aqi_listener.extract_args("/forecast 48"), ["48"])
        self.assertEqual(aqi_listener.extract_args("/forecast"), [])
        self.assertEqual(aqi_listener.extract_args("/now"), [])

    def test_format_slot(self):
        self.assertEqual(aqi_listener.format_slot("2026-08-29T15:00"), "Sat 15:00")

    def test_format_slot_survives_bad_input(self):
        self.assertEqual(aqi_listener.format_slot("nonsense"), "nonsense")

    def test_forecast_reports_peak_and_trough(self):
        text = aqi_listener.describe_forecast(
            self._points([90, 120, 205, 150, 80]), "Bukit Jalil", 24
        )
        self.assertIn("Peak:", text)
        self.assertIn("AQI 205", text)
        self.assertIn("Best:", text)
        self.assertIn("AQI 80", text)

    def test_forecast_advice_matches_the_peak_not_the_average(self):
        text = aqi_listener.describe_forecast(self._points([40, 40, 310, 40]), "X", 24)
        self.assertIn("Stay indoors", text)

    def test_forecast_handles_no_data(self):
        text = aqi_listener.describe_forecast([], "Bukit Jalil", 24)
        self.assertIn("Could not fetch", text)

    def test_timeline_is_capped_to_keep_the_message_readable(self):
        text = aqi_listener.describe_forecast(self._points([100] * 96), "X", 96)
        rows = [ln for ln in text.splitlines() if "AQI" not in ln and ":00" in ln]
        self.assertLessEqual(len(rows), aqi_listener.FORECAST_ROWS)

    def test_location_is_escaped(self):
        text = aqi_listener.describe_forecast(self._points([50]), "A & B", 24)
        self.assertIn("A &amp; B", text)

    def test_forecast_is_registered_as_a_command(self):
        self.assertIn("forecast", [name for name, _ in aqi_listener.COMMANDS])


class ChartDeliveryTests(unittest.TestCase):
    def _config(self):
        return dict(BASE_CONFIG)

    def test_text_commands_carry_no_chart(self):
        for command in ("help", "status", "where"):
            with self.subTest(command=command):
                _text, chart = aqi_listener.handle_command(command, self._config())
                self.assertIsNone(chart)

    def test_forecast_attaches_a_chart(self):
        from aqi_bot import ForecastPoint
        points = [ForecastPoint(time=f"2026-08-29T{h:02d}:00", aqi=100 + h) for h in range(12)]
        from aqi_bot import Forecast
        with mock.patch.object(aqi_listener, "fetch_forecast_detail",
                               return_value=Forecast(points=points, utc_offset_seconds=28800)), \
             mock.patch.object(aqi_listener, "render_chart", return_value=b"PNGDATA"):
            text, chart = aqi_listener.handle_command("forecast", self._config())
        self.assertEqual(chart, b"PNGDATA")
        self.assertIn("Forecast", text)

    def test_forecast_falls_back_to_text_when_rendering_fails(self):
        from aqi_bot import ForecastPoint
        points = [ForecastPoint(time=f"2026-08-29T{h:02d}:00", aqi=100 + h) for h in range(12)]
        from aqi_bot import Forecast
        with mock.patch.object(aqi_listener, "fetch_forecast_detail",
                               return_value=Forecast(points=points, utc_offset_seconds=28800)), \
             mock.patch.object(aqi_listener, "render_chart", return_value=None):
            text, chart = aqi_listener.handle_command("forecast", self._config())
        self.assertIsNone(chart)
        self.assertIn("Peak:", text)

    def test_render_chart_survives_a_broken_renderer(self):
        with mock.patch.dict("sys.modules", {"aqi_chart": mock.MagicMock(
                render_forecast_chart=mock.Mock(side_effect=ValueError("boom")))}):
            self.assertIsNone(aqi_listener.render_chart([], "X", "light"))

    def test_photo_is_sent_when_a_chart_exists(self):
        update = {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/forecast"}}
        with mock.patch.object(aqi_listener, "handle_command", return_value=("caption", b"PNG")), \
             mock.patch.object(aqi_listener, "send_photo") as photo, \
             mock.patch.object(aqi_listener, "send_telegram") as text:
            aqi_listener.process_update(update, BASE_CONFIG)
        photo.assert_called_once()
        text.assert_not_called()

    def test_text_is_sent_when_there_is_no_chart(self):
        update = {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "/help"}}
        with mock.patch.object(aqi_listener, "send_photo") as photo, \
             mock.patch.object(aqi_listener, "send_telegram") as text:
            aqi_listener.process_update(update, BASE_CONFIG)
        text.assert_called_once()
        photo.assert_not_called()


class ChartRenderingTests(unittest.TestCase):
    def _points(self, values):
        from aqi_bot import ForecastPoint
        import datetime as dt
        base = dt.datetime(2026, 8, 29, 7, 0)
        return [
            ForecastPoint(time=(base + dt.timedelta(hours=i)).isoformat(timespec="minutes"), aqi=v)
            for i, v in enumerate(values)
        ]

    def test_renders_a_png(self):
        from aqi_chart import render_forecast_chart
        png = render_forecast_chart(self._points([120, 140, 180, 160, 130]), "X")
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"), "output should be a PNG")

    def test_both_themes_render(self):
        from aqi_chart import render_forecast_chart
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                self.assertTrue(render_forecast_chart(self._points([90, 120, 150]), "X", theme))

    def test_unknown_theme_falls_back_to_light(self):
        from aqi_chart import render_forecast_chart
        self.assertTrue(render_forecast_chart(self._points([90, 120, 150]), "X", "chartreuse"))

    def test_too_few_points_renders_nothing(self):
        from aqi_chart import render_forecast_chart
        self.assertIsNone(render_forecast_chart(self._points([100]), "X"))
        self.assertIsNone(render_forecast_chart([], "X"))

    def test_unparseable_timestamps_render_nothing(self):
        from aqi_bot import ForecastPoint
        from aqi_chart import render_forecast_chart
        bad = [ForecastPoint(time="nope", aqi=100), ForecastPoint(time="also nope", aqi=110)]
        self.assertIsNone(render_forecast_chart(bad, "X"))

    def test_flat_series_does_not_collapse_the_axis(self):
        from aqi_chart import render_forecast_chart
        self.assertTrue(render_forecast_chart(self._points([100] * 12), "X"))


class NowMarkerTests(unittest.TestCase):
    def _points(self, count=12, start_hour=16):
        from aqi_bot import ForecastPoint
        import datetime as dt
        base = dt.datetime(2026, 8, 29, start_hour, 0)
        return [
            ForecastPoint(time=(base + dt.timedelta(hours=i)).isoformat(timespec="minutes"), aqi=150 + i)
            for i in range(count)
        ]

    def test_marker_inside_the_window_renders(self):
        import datetime as dt
        from aqi_chart import render_forecast_chart
        png = render_forecast_chart(self._points(), "X", now=dt.datetime(2026, 8, 29, 18, 30))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_marker_outside_the_window_is_skipped_not_crashed(self):
        import datetime as dt
        from aqi_chart import render_forecast_chart
        for stamp in (dt.datetime(2026, 8, 28, 0, 0), dt.datetime(2026, 9, 5, 0, 0)):
            with self.subTest(stamp=stamp):
                self.assertTrue(render_forecast_chart(self._points(), "X", now=stamp))

    def test_no_marker_when_now_is_unknown(self):
        from aqi_chart import render_forecast_chart
        self.assertTrue(render_forecast_chart(self._points(), "X", now=None))

    def test_marker_near_the_end_still_renders(self):
        import datetime as dt
        from aqi_chart import render_forecast_chart
        self.assertTrue(render_forecast_chart(self._points(), "X", now=dt.datetime(2026, 8, 30, 2, 50)))
