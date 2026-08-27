"""Unit tests for the band and hysteresis logic. Run: python3 -m unittest -v"""

import unittest

from aqi_bot import BANDS, band_index, compose_message, settle_band, Reading


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
