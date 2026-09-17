from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from solar_monitor.db import Database


class DailySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_a_partial_day_reports_its_coverage_and_only_the_energy_in_it(self):
        # Thirty minutes of readings every 30 s: PV flat at 100 W, the load
        # ramping 200 -> 400 W, the battery sagging 25.0 -> 24.4 V.
        start = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
        for index in range(61):
            self.db.save_sample((start + timedelta(seconds=30 * index)).isoformat(), {
                "pv_watts": 100,
                "ac_load_watts": 200 + 200 * index / 60,
                "battery_voltage": 25 - 0.01 * index,
            })
        rows = self.db.daily_summary("2026-09-10", 1)
        self.assertEqual(len(rows), 1)
        day = rows[0]
        self.assertEqual(day["day"], "2026-09-10")
        self.assertEqual(day["samples"], 61)
        self.assertAlmostEqual(day["coverage_hours"], 0.5, delta=0.01)
        # 100 W for half an hour, not 100 W * 24 h.
        self.assertAlmostEqual(day["pv_kwh"], 0.05, delta=0.001)
        # A linear ramp integrates exactly by the trapezoid rule: 300 W mean.
        self.assertAlmostEqual(day["load_kwh"], 0.15, delta=0.001)
        self.assertEqual(day["avg_pv_watts"], 100)
        self.assertEqual(day["peak_pv_watts"], 100)
        self.assertEqual(day["peak_load_watts"], 400)
        self.assertAlmostEqual(day["min_battery_voltage"], 24.4, places=6)

    def test_a_gap_is_integrated_as_five_minutes_whatever_its_length(self):
        self.db.save_sample("2026-09-10T10:00:00+00:00", {"pv_watts": 100})
        self.db.save_sample("2026-09-10T11:00:00+00:00", {"pv_watts": 300})
        day = self.db.daily_summary("2026-09-10", 1)[0]
        self.assertAlmostEqual(day["coverage_hours"], 5 / 60, delta=0.01)
        self.assertAlmostEqual(day["pv_kwh"], 200 * (5 / 60) / 1000, delta=0.001)

    def test_missing_days_come_back_as_zero_rows_in_calendar_order(self):
        self.db.save_sample("2026-09-08T12:00:00+00:00", {"pv_watts": 100, "ac_load_watts": 50})
        self.db.save_sample("2026-09-08T12:04:00+00:00", {"pv_watts": 300, "ac_load_watts": 150})
        rows = self.db.daily_summary("2026-09-10", 5)
        self.assertEqual(
            [row["day"] for row in rows],
            ["2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"],
        )
        for row in rows:
            if row["day"] == "2026-09-08":
                continue
            self.assertEqual(row["samples"], 0, row)
            self.assertEqual(row["coverage_hours"], 0, row)
            self.assertEqual(row["pv_kwh"], 0, row)
            self.assertEqual(row["load_kwh"], 0, row)
            self.assertIsNone(row["avg_pv_watts"], row)
            self.assertIsNone(row["peak_load_watts"], row)
            self.assertIsNone(row["min_battery_voltage"], row)
        with_data = rows[2]
        self.assertEqual(with_data["samples"], 2)
        self.assertAlmostEqual(with_data["pv_kwh"], 200 * (4 / 60) / 1000, delta=0.001)
        self.assertAlmostEqual(with_data["load_kwh"], 100 * (4 / 60) / 1000, delta=0.001)

    def test_days_are_local_calendar_days(self):
        # 23:30 UTC is already tomorrow two hours east of Greenwich and still
        # today five hours west of it.
        self.db.save_sample("2026-09-10T23:30:00+00:00", {"pv_watts": 100})
        east = self.db.daily_summary("2026-09-11", 2, utc_offset_minutes=120)
        self.assertEqual([(row["day"], row["samples"]) for row in east],
                         [("2026-09-10", 0), ("2026-09-11", 1)])
        west = self.db.daily_summary("2026-09-11", 2, utc_offset_minutes=-300)
        self.assertEqual([(row["day"], row["samples"]) for row in west],
                         [("2026-09-10", 1), ("2026-09-11", 0)])

    def test_the_first_reading_of_the_window_keeps_its_predecessor(self):
        # The interval that straddles the window's start is integrated, so a
        # day's figures do not depend on where the window happens to begin.
        self.db.save_sample("2026-09-09T23:58:00+00:00", {"pv_watts": 100})
        self.db.save_sample("2026-09-10T00:01:00+00:00", {"pv_watts": 300})
        rows = self.db.daily_summary("2026-09-10", 1)
        self.assertEqual([row["day"] for row in rows], ["2026-09-10"])
        self.assertEqual(rows[0]["samples"], 1)
        self.assertAlmostEqual(rows[0]["coverage_hours"], 3 / 60, delta=0.01)
        self.assertAlmostEqual(rows[0]["pv_kwh"], 200 * (3 / 60) / 1000, delta=0.001)

    def test_readings_outside_the_window_are_not_counted(self):
        self.db.save_sample("2026-09-05T12:00:00+00:00", {"pv_watts": 100})
        self.db.save_sample("2026-09-11T12:00:00+00:00", {"pv_watts": 100})
        rows = self.db.daily_summary("2026-09-10", 5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(row["samples"] for row in rows), 0)


if __name__ == "__main__":
    unittest.main()
