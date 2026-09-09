"""Unit tests for the band and hysteresis logic. Run: python3 -m unittest -v"""

import unittest

from aqi_bot import (BANDS, Reading, band_index, compose_message, pm25_lines,
                     pm25_to_us_aqi, settle_band, us_epa_aqi)


class BandIndexTests(unittest.TestCase):
    def test_boundaries_map_to_expected_bands(self):
        cases = [(0, 0), (50, 0), (51, 1), (100, 1), (101, 2), (150, 2),
                 (151, 3), (200, 3), (201, 4), (300, 4), (301, 5), (900, 5)]
        for aqi, expected in cases:
            with self.subTest(aqi=aqi):
                self.assertEqual(band_index(aqi), expected)


class SettleBandTests(unittest.TestCase):
    def test_first_ever_reading_takes_the_raw_band(self):
        self.assertEqual(settle_band(120, None, deadband=3), 2)

    def test_unchanged_band_is_kept(self):
        self.assertEqual(settle_band(120, 2, deadband=3), 2)

    def test_upward_move_needs_to_clear_the_boundary(self):
        # 101 is technically band 2 but only 1 point over the 101 boundary.
        self.assertEqual(settle_band(101, 1, deadband=3), 1)
        self.assertEqual(settle_band(104, 1, deadband=3), 2)

    def test_downward_move_needs_to_clear_the_boundary(self):
        # Dropping to 100 is band 1, but too close to the 100 ceiling.
        self.assertEqual(settle_band(100, 2, deadband=3), 2)
        self.assertEqual(settle_band(97, 2, deadband=3), 1)

    def test_hovering_at_a_boundary_does_not_flap(self):
        band = 1
        for aqi in [99, 101, 100, 102, 99, 101]:
            band = settle_band(aqi, band, deadband=3)
        self.assertEqual(band, 1, "readings hovering at 100 should not change band")

    def test_a_genuine_spike_still_gets_through(self):
        band = 1
        for aqi in [99, 101, 138, 175]:
            band = settle_band(aqi, band, deadband=3)
        self.assertEqual(band, 3)


class ComposeMessageTests(unittest.TestCase):
    def test_worsening_message_names_both_bands(self):
        reading = Reading(aqi=160, source="WAQI ground station", station="Cheras", pm25=71.0)
        text = compose_message(reading, band=3, previous_band=2, location="Home")
        self.assertIn("worsening", text)
        self.assertIn("AQI 160", text)
        self.assertIn("Unhealthy", text)
        self.assertIn("Up from Unhealthy for Sensitive Groups", text)
        self.assertIn("Cheras", text)

    def test_improving_message_reads_as_recovery(self):
        reading = Reading(aqi=80, source="Open-Meteo (modelled)")
        text = compose_message(reading, band=1, previous_band=3, location="Home")
        self.assertIn("improving", text)
        self.assertIn("Down from Unhealthy", text)

    def test_first_run_message_has_no_previous_band(self):
        reading = Reading(aqi=42, source="WAQI ground station")
        text = compose_message(reading, band=0, previous_band=None, location="Home")
        self.assertIn("Monitoring started", text)

    def test_every_band_has_advice(self):
        from aqi_bot import ADVICE
        for index in range(len(BANDS)):
            self.assertIn(index, ADVICE)


if __name__ == "__main__":
    unittest.main()


class RepeatIsDueTests(unittest.TestCase):
    from datetime import datetime, timedelta, timezone
    NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    def test_never_alerted_before_is_due(self):
        from aqi_bot import repeat_is_due
        self.assertTrue(repeat_is_due(None, 1, self.NOW))

    def test_disabled_when_repeat_hours_is_zero(self):
        from aqi_bot import repeat_is_due
        self.assertFalse(repeat_is_due("2026-08-27T06:00:00+00:00", 0, self.NOW))

    def test_not_due_shortly_after_an_alert(self):
        from aqi_bot import repeat_is_due
        self.assertFalse(repeat_is_due("2026-08-27T11:40:00+00:00", 1, self.NOW))

    def test_due_after_the_interval(self):
        from aqi_bot import repeat_is_due
        self.assertTrue(repeat_is_due("2026-08-27T11:00:00+00:00", 1, self.NOW))

    def test_timer_jitter_does_not_skip_an_hour(self):
        # Timer has RandomizedDelaySec=180, so a run can land ~3min early.
        from aqi_bot import repeat_is_due
        self.assertTrue(repeat_is_due("2026-08-27T11:02:00+00:00", 1, self.NOW))

    def test_naive_timestamp_is_treated_as_utc(self):
        from aqi_bot import repeat_is_due
        self.assertTrue(repeat_is_due("2026-08-27T11:00:00", 1, self.NOW))

    def test_corrupt_timestamp_falls_back_to_alerting(self):
        from aqi_bot import repeat_is_due
        self.assertTrue(repeat_is_due("not-a-date", 1, self.NOW))

    def test_multi_hour_interval_respected(self):
        from aqi_bot import repeat_is_due
        self.assertFalse(repeat_is_due("2026-08-27T10:30:00+00:00", 3, self.NOW))
        self.assertTrue(repeat_is_due("2026-08-27T09:00:00+00:00", 3, self.NOW))


class RepeatMessageTests(unittest.TestCase):
    def test_repeat_message_says_still_and_how_long(self):
        from aqi_bot import compose_message
        reading = Reading(aqi=172, source="WAQI ground station")
        text = compose_message(reading, 3, 3, "Bukit Jalil", repeat=True, band_age_seconds=5 * 3600)
        self.assertIn("Still Unhealthy", text)
        self.assertIn("AQI 172", text)
        self.assertIn("Ongoing for 5 hours", text)
        self.assertNotIn("worsening", text)

    def test_humanise_duration(self):
        from aqi_bot import humanise_duration
        self.assertEqual(humanise_duration(600), "under an hour")
        self.assertEqual(humanise_duration(3600), "1 hour")
        self.assertEqual(humanise_duration(5 * 3600), "5 hours")
        self.assertEqual(humanise_duration(72 * 3600), "3 days")

    def test_change_message_still_wins_over_repeat_wording(self):
        from aqi_bot import compose_message
        reading = Reading(aqi=210, source="WAQI ground station")
        text = compose_message(reading, 4, 3, "Bukit Jalil", repeat=False)
        self.assertIn("worsening", text)
        self.assertNotIn("Still", text)


class EpisodeScenarioTests(unittest.TestCase):
    """End-to-end: drive main() through an episode and assert what gets sent."""

    def _run_episode(self, series, repeat_hours="1"):
        import datetime as dt
        import os
        import tempfile
        from unittest import mock

        import aqi_bot

        state_path = os.path.join(tempfile.mkdtemp(), "state.json")
        env = {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "AQI_LAT": "3.05",
            "AQI_LON": "101.68", "AQI_LOCATION_NAME": "Bukit Jalil",
            "AQI_STATE_FILE": state_path, "AQI_ALERT_FLOOR_BAND": "2",
            "AQI_DEADBAND": "3", "AQI_REPEAT_HOURS": repeat_hours, "LOG_LEVEL": "CRITICAL",
        }
        start = dt.datetime(2026, 8, 27, 6, 0, tzinfo=dt.timezone.utc)
        clock = {"now": start}
        sent = []

        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(aqi_bot, "datetime", FakeDT), \
             mock.patch.object(aqi_bot, "send_telegram", lambda tk, c, text: sent.append(text)):
            for index, aqi in enumerate(series):
                clock["now"] = start + dt.timedelta(hours=index)
                with mock.patch.object(
                    aqi_bot, "get_reading",
                    return_value=Reading(aqi=aqi, source="WAQI ground station"),
                ):
                    aqi_bot.main()
        return sent

    def test_clean_air_is_entirely_silent(self):
        self.assertEqual(self._run_episode([30, 42, 55, 61, 48, 33]), [])

    def test_sustained_bad_air_repeats_every_hour(self):
        sent = self._run_episode([40, 130, 135, 140, 138])
        self.assertEqual(len(sent), 4, "one crossing plus three hourly repeats")
        self.assertIn("worsening", sent[0])
        for message in sent[1:]:
            self.assertIn("Still", message)

    def test_repeats_stop_once_air_recovers(self):
        sent = self._run_episode([130, 135, 60, 55, 50, 45])
        # crossing up, one repeat, then the recovery message, then silence.
        self.assertEqual(len(sent), 3)
        self.assertIn("improving", sent[-1])

    def test_repeat_hours_zero_restores_change_only_behaviour(self):
        sent = self._run_episode([40, 130, 135, 140, 138], repeat_hours="0")
        self.assertEqual(len(sent), 1, "only the crossing message")

    def test_wider_interval_sends_fewer_messages(self):
        sent = self._run_episode([130] * 7, repeat_hours="3")
        # hour 0 crossing, then repeats at +3h and +6h
        self.assertEqual(len(sent), 3)


class ForecastNowTests(unittest.TestCase):
    def test_offset_reconstructs_local_time_without_the_host_clock(self):
        from datetime import datetime, timezone
        from unittest import mock

        import aqi_bot
        from aqi_bot import Forecast

        fixed_utc = datetime(2026, 8, 29, 8, 32, tzinfo=timezone.utc)

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc

        with mock.patch.object(aqi_bot, "datetime", FakeDT):
            local = Forecast(points=[], utc_offset_seconds=28800).current_local_time()
        self.assertEqual(local.hour, 16, "UTC+8 should read 16:32 local")
        self.assertEqual(local.minute, 32)
        self.assertIsNone(local.tzinfo, "must stay naive to match the API's local stamps")

    def test_missing_offset_yields_no_marker(self):
        from aqi_bot import Forecast
        self.assertIsNone(Forecast(points=[], utc_offset_seconds=None).current_local_time())

    def test_fetch_forecast_still_returns_a_bare_list(self):
        from unittest import mock

        import aqi_bot
        from aqi_bot import Forecast, ForecastPoint

        sample = Forecast(points=[ForecastPoint(time="2026-08-29T16:00", aqi=100)], now="x")
        with mock.patch.object(aqi_bot, "fetch_forecast_detail", return_value=sample):
            result = aqi_bot.fetch_forecast(1.0, 2.0, 12)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0].aqi, 100)


class RelativePhraseTests(unittest.TestCase):
    import datetime as _dt
    NOW = _dt.datetime(2026, 8, 29, 16, 30)

    def phrase(self, **delta):
        import datetime as dt
        from aqi_bot import relative_phrase
        return relative_phrase(self.NOW + dt.timedelta(**delta), self.NOW)

    def test_imminent(self):
        self.assertEqual(self.phrase(minutes=5), "right about now")

    def test_minutes_round_to_ten(self):
        self.assertEqual(self.phrase(minutes=28), "in about 30 minutes")
        self.assertEqual(self.phrase(minutes=43), "in about 40 minutes")

    def test_about_an_hour(self):
        self.assertEqual(self.phrase(minutes=65), "in about an hour")

    def test_hours_stay_bare_when_near(self):
        self.assertEqual(self.phrase(hours=3), "in about 3 hours")
        self.assertNotIn("(", self.phrase(hours=5))

    def test_distant_hours_gain_a_clock_time(self):
        # "in about 14 hours" alone is hard to picture, so anchor it.
        self.assertEqual(self.phrase(hours=14), "in about 14 hours (06:30)")

    def test_about_a_day(self):
        self.assertIn("about a day", self.phrase(hours=26))

    def test_multiple_days(self):
        self.assertIn("about 3 days", self.phrase(hours=72))

    def test_past_targets_read_as_now(self):
        self.assertEqual(self.phrase(hours=-2), "right about now")

    def test_unknown_now_falls_back_to_the_clock(self):
        import datetime as dt
        from aqi_bot import relative_phrase
        self.assertEqual(relative_phrase(dt.datetime(2026, 8, 29, 19, 0), None), "at 19:00")


class RelativePhraseDayAnchorTests(unittest.TestCase):
    import datetime as _dt
    NOW = _dt.datetime(2026, 8, 29, 16, 30)  # a Saturday

    def phrase(self, **delta):
        import datetime as dt
        from aqi_bot import relative_phrase
        return relative_phrase(self.NOW + dt.timedelta(**delta), self.NOW)

    def test_same_day_anchor_omits_the_weekday(self):
        self.assertEqual(self.phrase(hours=14), "in about 14 hours (06:30)")

    def test_day_scale_anchor_names_the_weekday(self):
        # "(20:30)" alone would not say which day.
        self.assertEqual(self.phrase(hours=28), "in about a day (Sun 20:30)")

    def test_multi_day_anchor_names_the_weekday(self):
        self.assertIn("(Tue", self.phrase(hours=72))

    def test_threshold_boundary(self):
        self.assertNotIn("Sun", self.phrase(hours=22))
        self.assertIn("Sun", self.phrase(hours=23))


class Pm25ToUsAqiTests(unittest.TestCase):
    def test_breakpoint_edges_land_on_band_edges(self):
        cases = [(0.0, 0), (9.0, 50), (9.1, 51), (35.4, 100),
                 (35.5, 101), (55.4, 150), (55.5, 151), (125.4, 200)]
        for concentration, expected in cases:
            with self.subTest(concentration=concentration):
                self.assertEqual(pm25_to_us_aqi(concentration), expected)

    def test_interpolates_within_a_band(self):
        # Midway through 9.1-35.4 should be midway through 51-100.
        self.assertEqual(pm25_to_us_aqi((9.1 + 35.4) / 2), 76)

    def test_off_the_top_of_the_scale_clamps(self):
        self.assertEqual(pm25_to_us_aqi(9999), 500)

    def test_negative_concentration_is_rejected(self):
        with self.assertRaises(ValueError):
            pm25_to_us_aqi(-1)


class UsEpaAqiTests(unittest.TestCase):
    """The Malaysian DOE passthrough, and the shape every other country sends."""

    MALAYSIAN = {
        "aqi": {"v": 77}, "pm25": {"v": 141}, "pm10": {"v": 40},
        "co": {"v": 0.1}, "no2": {"v": 8.9}, "o3": {"v": 4.2}, "so2": {"v": 1.5},
    }
    PLAIN = {"pm25": {"v": 93}, "pm10": {"v": 35}, "o3": {"v": 12}}

    def test_national_index_passthrough_is_corrected_to_the_sub_index(self):
        # Putrajaya really reported these: headline 77 (Malaysian API) while its
        # own PM2.5 sub-index was 141 (US EPA) - Moderate printed for USG.
        self.assertEqual(us_epa_aqi(77, self.MALAYSIAN), 141)

    def test_the_correction_crosses_the_alert_floor(self):
        # The default AQI_ALERT_FLOOR_BAND is 2, so this is the difference
        # between total silence and an alert, not merely a wrong label.
        self.assertEqual(band_index(77), 1)
        self.assertEqual(band_index(us_epa_aqi(77, self.MALAYSIAN)), 2)

    def test_a_normal_station_is_left_alone(self):
        self.assertEqual(us_epa_aqi(93, self.PLAIN), 93)

    def test_headline_above_the_sub_indices_is_kept(self):
        # Ozone or another pollutant can legitimately drive the composite.
        self.assertEqual(us_epa_aqi(120, self.PLAIN), 120)

    def test_gas_concentrations_never_raise_the_result(self):
        # A CO of 0.1 is ppm, not a sub-index; only pm25/pm10 are comparable.
        only_gases = {"co": {"v": 0.1}, "no2": {"v": 8.9}}
        self.assertEqual(us_epa_aqi(77, only_gases), 77)

    def test_missing_iaqi_falls_back_to_the_headline(self):
        self.assertEqual(us_epa_aqi(64, {}), 64)

    def test_non_numeric_sub_indices_are_ignored(self):
        self.assertEqual(us_epa_aqi(50, {"pm25": {"v": "-"}}), 50)

    def test_pm10_can_drive_the_correction(self):
        self.assertEqual(us_epa_aqi(60, {"pm10": {"v": 118}}), 118)


class Pm25LinesTests(unittest.TestCase):
    def test_a_concentration_is_labelled_with_its_unit(self):
        reading = Reading(aqi=101, source="Open-Meteo (modelled)", pm25=41.6)
        self.assertEqual(pm25_lines(reading), ["PM2.5: 41.6 ug/m3"])

    def test_a_sub_index_is_labelled_as_a_sub_index(self):
        reading = Reading(aqi=141, source="WAQI ground station", pm25_index=141)
        self.assertEqual(pm25_lines(reading), ["PM2.5 sub-index: 141"])

    def test_neither_renders_nothing(self):
        self.assertEqual(pm25_lines(Reading(aqi=50, source="x")), [])
