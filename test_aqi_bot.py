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
