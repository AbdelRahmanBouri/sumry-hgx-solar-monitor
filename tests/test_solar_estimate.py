import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from solar_monitor import solar_estimate


class SolarPositionTests(unittest.TestCase):
    def test_midday_equinox_is_near_zenith_at_the_equator(self):
        # March equinox, solar noon at 0,0: the sun should be almost directly overhead.
        when = datetime(2025, 3, 20, 12, 8, tzinfo=timezone.utc)
        elevation, _azimuth = solar_estimate.solar_position(0.0, 0.0, when)
        self.assertGreater(elevation, 85)

    def test_local_midnight_is_below_horizon(self):
        # 40N, 105W: solar noon falls near 19:00 UTC, so solar midnight (and
        # the deepest night) falls near 07:00 UTC.
        when = datetime(2025, 6, 21, 7, 0, tzinfo=timezone.utc)
        elevation, _azimuth = solar_estimate.solar_position(40.0, -105.0, when)
        self.assertLess(elevation, 0)

    def test_elevation_range_is_physical(self):
        when = datetime(2025, 9, 3, 15, 30, tzinfo=timezone.utc)
        elevation, azimuth = solar_estimate.solar_position(34.05, -118.24, when)
        self.assertTrue(-90 <= elevation <= 90)
        self.assertTrue(0 <= azimuth <= 360)

    def test_late_afternoon_sun_is_in_the_west(self):
        # 17:30 PDT is 00:30 UTC on the following date. This specifically
        # guards the true-solar-time midnight wrap used by azimuth.
        when = datetime(2026, 9, 4, 0, 30, tzinfo=timezone.utc)
        elevation, azimuth = solar_estimate.solar_position(34.05, -118.24, when)
        self.assertGreater(elevation, 0)
        self.assertTrue(180 < azimuth < 300)


class ClearSkyGhiTests(unittest.TestCase):
    def test_zero_below_horizon(self):
        self.assertEqual(solar_estimate.clear_sky_ghi(-5), 0.0)
        self.assertEqual(solar_estimate.clear_sky_ghi(0), 0.0)

    def test_increases_with_elevation(self):
        low = solar_estimate.clear_sky_ghi(15)
        high = solar_estimate.clear_sky_ghi(80)
        self.assertGreater(high, low)
        self.assertGreater(low, 0)

    def test_near_solar_constant_at_zenith(self):
        # Haurwitz caps out below the solar constant even at 90 degrees, but
        # should be in a physically sane ballpark for a clear sky.
        overhead = solar_estimate.clear_sky_ghi(90)
        self.assertTrue(900 < overhead < 1100)


class CloudAttenuationTests(unittest.TestCase):
    def test_clear_sky_passes_almost_everything(self):
        self.assertAlmostEqual(solar_estimate.cloud_attenuation(0), 1.0, places=2)

    def test_overcast_sky_is_heavily_attenuated_but_not_zero(self):
        factor = solar_estimate.cloud_attenuation(100)
        self.assertTrue(0 < factor < 0.3)

    def test_monotonically_decreasing(self):
        values = [solar_estimate.cloud_attenuation(pct) for pct in (0, 25, 50, 75, 100)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_clamps_out_of_range_inputs(self):
        self.assertEqual(solar_estimate.cloud_attenuation(-10), solar_estimate.cloud_attenuation(0))
        self.assertEqual(solar_estimate.cloud_attenuation(150), solar_estimate.cloud_attenuation(100))


class PlaneOfArrayTests(unittest.TestCase):
    LAT, LON = 34.05, -118.24

    def test_diffuse_fraction_moves_the_right_way(self):
        overcast = solar_estimate.erbs_diffuse_fraction(0.1)
        clear = solar_estimate.erbs_diffuse_fraction(0.75)
        self.assertGreater(overcast, 0.9, "an overcast sky is nearly all diffuse")
        self.assertLess(clear, 0.5, "a clear sky is mostly direct beam")

    def test_a_flat_panel_matches_horizontal_irradiance(self):
        # Tilt zero adds no ground reflection and sees the whole sky, so the
        # transposition must collapse back to plain GHI.
        elevation, azimuth = solar_estimate.solar_position(
            self.LAT, self.LON, datetime(2026, 9, 3, 19, 26, tzinfo=timezone.utc)
        )
        ghi = solar_estimate.clear_sky_ghi(elevation)
        plane = solar_estimate.plane_of_array_irradiance(elevation, azimuth, ghi, 0, 180)
        self.assertAlmostEqual(plane, ghi, delta=ghi * 0.01)

    def test_tilt_favours_low_sun_and_costs_a_little_at_high_sun(self):
        noon = datetime(2026, 12, 21, 20, 0, tzinfo=timezone.utc)   # winter, low sun
        elevation, azimuth = solar_estimate.solar_position(self.LAT, self.LON, noon)
        ghi = solar_estimate.clear_sky_ghi(elevation)
        flat = solar_estimate.plane_of_array_irradiance(elevation, azimuth, ghi, 0, 185)
        tilted = solar_estimate.plane_of_array_irradiance(elevation, azimuth, ghi, 45, 185)
        self.assertGreater(tilted, flat * 1.2, "a 45 degree array beats flat in winter")

    def test_facing_away_from_the_sun_loses_the_beam(self):
        when = datetime(2026, 9, 3, 19, 26, tzinfo=timezone.utc)
        elevation, azimuth = solar_estimate.solar_position(self.LAT, self.LON, when)
        ghi = solar_estimate.clear_sky_ghi(elevation)
        south = solar_estimate.plane_of_array_irradiance(elevation, azimuth, ghi, 45, 185)
        north = solar_estimate.plane_of_array_irradiance(elevation, azimuth, ghi, 45, 5)
        self.assertGreater(south, north * 1.5)
        self.assertGreater(north, 0, "a shaded panel still collects diffuse and ground light")

    def test_series_honours_the_array_orientation(self):
        when = datetime(2026, 12, 21, 20, 0, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            flat, _ = solar_estimate.estimate_series(
                self.LAT, self.LON, 2640, 0.8, when, when, step_minutes=10,
            )
            tilted, _ = solar_estimate.estimate_series(
                self.LAT, self.LON, 2640, 0.8, when, when, step_minutes=10,
                tilt_degrees=45, azimuth_degrees=185,
            )
        self.assertGreater(tilted[0]["estimated_watts"], flat[0]["estimated_watts"])
        self.assertIn("azimuth_deg", tilted[0])

    def test_a_gable_split_lands_between_its_two_faces(self):
        when = datetime(2026, 9, 3, 19, 26, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            def watts(**kw):
                points, _ = solar_estimate.estimate_series(
                    self.LAT, self.LON, 2640, 0.74, when, when, step_minutes=10, **kw
                )
                return points[0]["estimated_watts"]
            south = watts(tilt_degrees=14, azimuth_degrees=185)
            north = watts(tilt_degrees=14, azimuth_degrees=5)
            split = watts(tilt_degrees=14, azimuth_degrees=185, dual_slope=True)
        self.assertLess(split, south)
        self.assertGreater(split, north)
        self.assertAlmostEqual(split, (south + north) / 2, delta=1)

    def test_a_symmetric_gable_is_close_to_flat_whatever_the_pitch(self):
        # Opposite faces cancel, which is why the roof pitch is a low-stakes
        # input: this is the guard against re-introducing a steep single-plane
        # "fit" that morning-and-evening-only data would happily support.
        when = datetime(2026, 9, 3, 19, 26, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            flat, _ = solar_estimate.estimate_series(
                self.LAT, self.LON, 2640, 0.74, when, when, step_minutes=10,
            )
            for pitch in (5, 10, 14, 20, 25):
                split, _ = solar_estimate.estimate_series(
                    self.LAT, self.LON, 2640, 0.74, when, when, step_minutes=10,
                    tilt_degrees=pitch, azimuth_degrees=185, dual_slope=True,
                )
                ratio = split[0]["estimated_watts"] / flat[0]["estimated_watts"]
                self.assertTrue(0.9 <= ratio <= 1.1, f"pitch {pitch} gave {ratio:.2f}x flat")

    def test_matched_faces_lose_nothing_to_string_mismatch(self):
        self.assertEqual(solar_estimate.series_mismatch_factor(800, 800), 1.0)
        self.assertEqual(solar_estimate.series_mismatch_factor(0, 0), 1.0)

    def test_mismatch_bites_hardest_at_a_middling_irradiance_split(self):
        # Below roughly a 0.45 ratio the bypass diodes drop the weak pair and
        # the string recovers, so the curve is a valley, not a slide.
        worst = solar_estimate.series_mismatch_factor(360, 800)
        milder = solar_estimate.series_mismatch_factor(720, 800)
        very_dark = solar_estimate.series_mismatch_factor(40, 800)
        self.assertLess(worst, milder)
        self.assertLess(worst, very_dark)
        self.assertTrue(0.6 < worst < 0.75)

    def test_straddling_strings_cost_output_against_per_face_strings(self):
        when = datetime(2026, 12, 21, 20, 0, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            def watts(split):
                points, _ = solar_estimate.estimate_series(
                    self.LAT, self.LON, 2640, 0.77, when, when, step_minutes=10,
                    tilt_degrees=14, azimuth_degrees=185, dual_slope=True,
                    split_strings=split,
                )
                return points[0]["estimated_watts"]
        self.assertLess(watts(True), watts(False))

    def test_output_never_exceeds_the_array_by_more_than_a_hair(self):
        when = datetime(2026, 6, 21, 19, 30, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            points, _ = solar_estimate.estimate_series(
                self.LAT, self.LON, 2640, 0.8, when, when, step_minutes=10,
                tilt_degrees=45, azimuth_degrees=185,
            )
        self.assertLessEqual(points[0]["estimated_watts"], 2640 * 1.05)


class LearnedEfficiencyTests(unittest.TestCase):
    LAT, LON = 34.05, -118.24
    GEOMETRY = dict(tilt_degrees=14, azimuth_degrees=185, dual_slope=True)

    def synthetic_day(self, day, factor, volts=25.5, amps=30.0, every_minutes=2):
        """A day of readings from an array delivering `factor` of the clear-sky
        model: 13:00 to 23:00 UTC is dawn to dusk at the site."""
        samples = []
        when = datetime(2026, 9, day, 13, 0, tzinfo=timezone.utc)
        end = when.replace(hour=23)
        while when < end:
            elevation, azimuth = solar_estimate.solar_position(self.LAT, self.LON, when)
            plane = solar_estimate.array_irradiance(
                elevation, azimuth, solar_estimate.clear_sky_ghi(elevation), 14, 185, True,
            )
            samples.append({
                "captured_at": when.isoformat(),
                "pv_watts": round(2640 * plane / 1000 * factor, 1),
                "battery_voltage": volts,
                "battery_current": amps,
            })
            when += timedelta(minutes=every_minutes)
        return samples

    def test_recovers_the_factor_the_readings_were_made_with(self):
        learned = solar_estimate.learn_efficiency(
            self.synthetic_day(3, 0.85), self.LAT, self.LON, 2640, **self.GEOMETRY,
        )
        self.assertIsNotNone(learned)
        for band, value in learned["bands"].items():
            self.assertAlmostEqual(value, 0.85, delta=0.01, msg=f"band {band}")
        self.assertEqual(learned["days"], 1)
        self.assertGreater(learned["samples"], solar_estimate.MIN_TOTAL_SAMPLES)

    def test_throttled_readings_are_ignored(self):
        # A full bank holding at float takes a trickle no matter what the
        # panels could make; those readings would drag the curve down.
        free = self.synthetic_day(3, 0.85)
        throttled = self.synthetic_day(4, 0.35, volts=27.0, amps=2.0)
        learned = solar_estimate.learn_efficiency(
            free + throttled, self.LAT, self.LON, 2640, **self.GEOMETRY,
        )
        for value in learned["bands"].values():
            self.assertAlmostEqual(value, 0.85, delta=0.01)

    def test_a_heavy_load_below_the_plateau_still_counts(self):
        # Little current reaching the battery because the home is using the
        # solar is not the charger throttling; only holding-plus-trickle is.
        learned = solar_estimate.learn_efficiency(
            self.synthetic_day(3, 0.85, volts=25.0, amps=1.0),
            self.LAT, self.LON, 2640, **self.GEOMETRY,
        )
        self.assertIsNotNone(learned)

    def test_passing_cloud_does_not_lower_the_envelope(self):
        clear = self.synthetic_day(3, 0.85)
        # A quarter of the readings at half output, as a cloud would leave them.
        for index, sample in enumerate(clear):
            if index % 4 == 0:
                sample["pv_watts"] = round(sample["pv_watts"] * 0.5, 1)
        learned = solar_estimate.learn_efficiency(clear, self.LAT, self.LON, 2640, **self.GEOMETRY)
        for value in learned["bands"].values():
            self.assertAlmostEqual(value, 0.85, delta=0.01)

    def test_too_little_data_means_no_curve(self):
        few = self.synthetic_day(3, 0.85)[:10]
        self.assertIsNone(solar_estimate.learn_efficiency(few, self.LAT, self.LON, 2640, **self.GEOMETRY))
        self.assertIsNone(solar_estimate.learn_efficiency([], self.LAT, self.LON, 2640, **self.GEOMETRY))

    def test_efficiency_interpolates_between_bands_and_holds_beyond_them(self):
        bands = {"10": 0.7, "20": 0.8, "30": 0.9}
        self.assertEqual(solar_estimate.efficiency_at(bands, 2), 0.7)
        self.assertAlmostEqual(solar_estimate.efficiency_at(bands, 20), 0.75)
        self.assertEqual(solar_estimate.efficiency_at(bands, 60), 0.9)
        self.assertEqual(solar_estimate.efficiency_at({}, 45), 1.0)

    def test_a_learned_curve_sets_the_level_of_the_series(self):
        when = datetime(2026, 9, 3, 19, 26, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            def watts(**kw):
                points, _ = solar_estimate.estimate_series(
                    self.LAT, self.LON, 2640, 0.5, when, when, step_minutes=10,
                    tilt_degrees=14, azimuth_degrees=185, dual_slope=True, **kw,
                )
                return points[0]["estimated_watts"]
            flat_ratio = watts()
            learned = watts(efficiency={"50": 1.0})
        self.assertAlmostEqual(learned, flat_ratio * 2, delta=1)


class EstimateSeriesTests(unittest.TestCase):
    def test_falls_back_to_clear_sky_without_weather(self):
        # No network (or an unreachable host) should degrade gracefully
        # rather than raising; mocked here so the test is fast and offline.
        start = datetime(2025, 9, 3, 6, 0, tzinfo=timezone.utc)
        end = datetime(2025, 9, 3, 8, 0, tzinfo=timezone.utc)
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            points, source = solar_estimate.estimate_series(
                34.05, -118.24, capacity_watts=600, performance_ratio=0.8,
                start=start, end=end, step_minutes=30,
            )
        self.assertEqual(source, "clear-sky-only")
        self.assertTrue(points)
        for point in points:
            self.assertGreaterEqual(point["estimated_watts"], 0)
            self.assertLessEqual(point["estimated_watts"], 600 * 1.05)

    def test_cloud_cover_reduces_the_estimate(self):
        start = datetime(2025, 9, 3, 18, 0, tzinfo=timezone.utc)  # midday in LA
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value={"2025-09-03T18:00": 100}):
            overcast, source = solar_estimate.estimate_series(
                34.05, -118.24, capacity_watts=600, performance_ratio=0.8,
                start=start, end=start, step_minutes=10,
            )
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value={"2025-09-03T18:00": 0}):
            clear, _source = solar_estimate.estimate_series(
                34.05, -118.24, capacity_watts=600, performance_ratio=0.8,
                start=start, end=start, step_minutes=10,
            )
        self.assertEqual(source, "open-meteo")
        self.assertLess(overcast[0]["estimated_watts"], clear[0]["estimated_watts"])

    def test_nighttime_output_is_zero(self):
        midnight = datetime(2025, 9, 3, 8, 0, tzinfo=timezone.utc)  # ~1am Pacific
        with patch.object(solar_estimate, "fetch_cloud_cover", return_value=None):
            points, _source = solar_estimate.estimate_series(
                34.05, -118.24, capacity_watts=600, performance_ratio=0.8,
                start=midnight, end=midnight, step_minutes=10,
            )
        self.assertEqual(points[0]["estimated_watts"], 0)


if __name__ == "__main__":
    unittest.main()
