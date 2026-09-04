import unittest
from datetime import datetime, timedelta, timezone

from solar_monitor import battery


def readings(start, minutes, volts, amps, pv=0.0, every=1):
    """`minutes` of steady readings from `start`, one per `every` minutes."""
    rows = []
    for minute in range(0, minutes, every):
        rows.append({
            "captured_at": (start + timedelta(minutes=minute)).isoformat(),
            "battery_voltage": volts, "battery_current": amps, "pv_watts": pv,
        })
    return rows


T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


class RestVoltageTests(unittest.TestCase):
    def test_follows_the_odyssey_curve(self):
        # 25.68 V is 12.84 per 12 V, the curve's top anchor -- but only to
        # within floating point, so this is an almost-equal by necessity.
        self.assertAlmostEqual(battery.soc_from_rest_voltage(25.68), 100.0, places=6)
        self.assertAlmostEqual(battery.soc_from_rest_voltage(24.36), 50.0, places=1)
        self.assertAlmostEqual(battery.soc_from_rest_voltage(23.16), 0.0, places=6)
        self.assertEqual(battery.soc_from_rest_voltage(21.0), 0.0)
        self.assertEqual(battery.soc_from_rest_voltage(28.0), 100.0)

    def test_is_monotonic_across_the_range(self):
        previous = -1
        for tenth in range(210, 290):
            soc = battery.soc_from_rest_voltage(tenth / 10)
            self.assertGreaterEqual(soc, previous, f"{tenth / 10} V went backwards")
            previous = soc


class OpenCircuitCorrectionTests(unittest.TestCase):
    def test_discharge_reads_low_and_charge_reads_high(self):
        self.assertGreater(battery.open_circuit_voltage(24.0, -4.0), 24.0)
        self.assertLess(battery.open_circuit_voltage(24.0, 4.0), 24.0)
        self.assertEqual(battery.open_circuit_voltage(24.0, 0.0), 24.0)

    def test_the_correction_is_small_at_the_loads_a_rest_allows(self):
        # 4 A on this bank is C/50; the correction must be millivolts, not volts.
        self.assertAlmostEqual(battery.open_circuit_voltage(24.0, -4.0), 24.012, places=3)


class CoulombCountTests(unittest.TestCase):
    def test_no_anchor_means_no_figure(self):
        rows = readings(T0, 120, 25.0, -8.0)
        state = battery.count_charge(rows, 200)
        self.assertIsNone(state["soc"])
        self.assertEqual(state["method"], "unavailable")

    def test_a_full_anchor_then_discharge_counts_down(self):
        rows = readings(T0, 15, 27.2, 3.0)                       # held at float, trickle: full
        rows += readings(T0 + timedelta(minutes=15), 120, 25.0, -10.0)  # 20 Ah out
        state = battery.count_charge(rows, 200)
        self.assertEqual(state["method"], "coulomb")
        self.assertAlmostEqual(state["soc"], 90.0, delta=0.3)
        self.assertEqual(len(state["full_events"]), 1)
        self.assertAlmostEqual(state["ah_out_since_full"], 20.0, delta=0.3)
        self.assertEqual(state["capacity_source"], "nameplate")

    def test_charge_is_counted_at_the_chemistrys_efficiency(self):
        rows = readings(T0, 40, 24.36, 0.5)                      # resting at 50%
        rows += readings(T0 + timedelta(minutes=40), 60, 26.0, 20.0)  # 20 Ah pushed in
        state = battery.count_charge(rows, 200)
        self.assertEqual(len(state["rest_events"]), 1)
        self.assertAlmostEqual(state["soc"], 50 + 20 * battery.CHARGE_EFFICIENCY / 200 * 100, delta=0.4)

    def test_a_full_to_rest_stretch_measures_the_capacity(self):
        rows = readings(T0, 15, 27.2, 3.0)                             # full
        rows += readings(T0 + timedelta(minutes=15), 300, 24.8, -10.0)  # 50 Ah out
        rows += readings(T0 + timedelta(minutes=315), 40, 24.36, 0.3)   # rests at 50%
        state = battery.count_charge(rows, 200)
        # 50 Ah emptied half the bank, so it holds about 100 Ah; blended
        # halfway toward that from the 200 Ah nameplate.
        self.assertEqual(state["capacity_source"], "measured")
        self.assertAlmostEqual(state["capacity_measurements"][0]["measured_ah"], 100, delta=2)
        self.assertAlmostEqual(state["capacity_ah"], 150, delta=2)
        self.assertAlmostEqual(state["soc"], 50, delta=0.5)

    def test_a_realistic_overnight_load_still_anchors(self):
        """This installation never drops below 1.5 A for 30 minutes -- its
        quietest stretch sits at 3-4 A. A fixed low threshold would mean the
        capacity was never measured at all, so the anchor is a C-rate."""
        rows = readings(T0, 15, 27.2, 3.0)                              # full
        rows += readings(T0 + timedelta(minutes=15), 300, 24.8, -12.0)   # 60 Ah out
        rows += readings(T0 + timedelta(minutes=315), 40, 24.36, -3.5)   # light overnight load
        state = battery.count_charge(rows, 200)
        self.assertEqual(len(state["rest_events"]), 1, "a C/50 load must still count as rest")
        self.assertEqual(state["capacity_source"], "measured")

    def test_a_heavy_load_is_not_mistaken_for_rest(self):
        rows = readings(T0, 15, 27.2, 3.0)
        rows += readings(T0 + timedelta(minutes=15), 300, 24.0, -25.0)  # well above C/50
        state = battery.count_charge(rows, 200)
        self.assertEqual(state["rest_events"], [])

    def test_a_shallow_stretch_does_not_learn(self):
        rows = readings(T0, 15, 27.2, 3.0)
        rows += readings(T0 + timedelta(minutes=15), 60, 25.4, -10.0)   # only 10 Ah of 200
        rows += readings(T0 + timedelta(minutes=75), 40, 25.3, 0.3)
        state = battery.count_charge(rows, 200)
        self.assertEqual(state["capacity_source"], "nameplate")
        self.assertEqual(state["capacity_measurements"], [])

    def test_a_gap_in_the_data_is_not_integrated_across(self):
        rows = readings(T0, 15, 27.2, 3.0)
        rows += readings(T0 + timedelta(hours=3), 10, 25.0, -10.0)  # 3 h later, 10 min of load
        state = battery.count_charge(rows, 200)
        # Only the 10 minutes actually observed count: ~1.5 Ah, not 30.
        self.assertGreater(state["soc"], 98.5)

    def test_absorption_current_does_not_count_as_full(self):
        # Holding the plateau but still pushing 20 A: still charging, not full.
        rows = readings(T0, 60, 28.2, 20.0)
        state = battery.count_charge(rows, 200)
        self.assertIsNone(state["soc"])
        self.assertEqual(state["full_events"], [])


class DailyHealthTests(unittest.TestCase):
    def synthetic_day(self):
        day = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
        rows = readings(day, 360, 23.8, -3.0, pv=0)                                   # night to 06:00
        rows += readings(day + timedelta(hours=6), 240, 26.0, 30.0, pv=900)           # bulk to 10:00
        # Absorption: at the setpoint from 10:00, current tapering 20 -> 4 A, until 12:00.
        for minute in range(120):
            rows.append({
                "captured_at": (day + timedelta(hours=10, minutes=minute)).isoformat(),
                "battery_voltage": 28.2, "battery_current": 20 - minute * (16 / 119), "pv_watts": 1200,
            })
        rows += readings(day + timedelta(hours=12), 300, 27.0, 2.0, pv=800)          # float to 17:00
        rows += readings(day + timedelta(hours=17), 420, 24.5, -5.0, pv=0)            # evening
        return rows

    def test_metrics_for_a_synthetic_day(self):
        days = battery.daily_health(self.synthetic_day(), bulk_setpoint_v=28.2)
        self.assertEqual(len(days), 1)
        today = days[0]
        self.assertEqual(today["day"], "2026-09-04")
        self.assertEqual(today["predawn_volts"], 23.8)
        self.assertEqual(today["predawn_amps"], -3.0)
        self.assertTrue(today["absorption_reached"])
        self.assertTrue(today["absorption_ended"])
        self.assertAlmostEqual(today["absorption_minutes"], 120, delta=2)
        # The taper ends at 4.0 A; the tail is the median of the last five
        # readings, so it sits just above that rather than exactly on it.
        self.assertAlmostEqual(today["tail_amps"], 4.3, delta=0.2)
        # 4 h at 30 A + the taper + 5 h at 2 A in; 6 h at 3 A + 7 h at 5 A out.
        self.assertAlmostEqual(today["ah_in"], 120 + 24 + 10, delta=2)
        self.assertAlmostEqual(today["ah_out"], 18 + 35, delta=1)

    def test_a_transient_touch_of_the_setpoint_is_not_an_absorption(self):
        """The regression this guards: taking the first crossing of the band
        reported 3 minutes and a 43 A tail for a hold that in the real data
        ran over an hour and tapered to 4 A."""
        day = datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc)
        rows = readings(day, 360, 23.8, -3.0)
        rows += readings(day + timedelta(hours=6), 2, 28.2, 43.0, pv=900)     # transient spike
        rows += readings(day + timedelta(hours=6, minutes=2), 178, 26.5, 40.0, pv=900)  # bulk
        # The real hold: 70 minutes at the setpoint, tapering 9 -> 4 A.
        for minute in range(70):
            rows.append({
                "captured_at": (day + timedelta(hours=9, minutes=minute)).isoformat(),
                "battery_voltage": 28.18, "battery_current": 9 - minute * (5 / 69), "pv_watts": 700,
            })
        rows += readings(day + timedelta(hours=10, minutes=10), 300, 27.0, 3.0, pv=600)
        today = battery.daily_health(rows, bulk_setpoint_v=28.2)[0]
        self.assertTrue(today["absorption_reached"])
        self.assertTrue(today["absorption_ended"])
        self.assertAlmostEqual(today["absorption_minutes"], 69, delta=2)
        self.assertAlmostEqual(today["tail_amps"], 4.1, delta=0.4)

    def test_a_hold_still_running_at_the_end_of_the_data_is_not_ended(self):
        day = datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)
        rows = readings(day, 60, 26.0, 30.0, pv=900) + readings(day + timedelta(hours=1), 40, 28.2, 12.0, pv=900)
        today = battery.daily_health(rows, bulk_setpoint_v=28.2)[0]
        self.assertTrue(today["absorption_reached"])
        self.assertFalse(today["absorption_ended"])

    def test_a_day_that_never_reaches_absorption_says_so(self):
        day = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        rows = readings(day, 360, 23.5, -2.0) + readings(day + timedelta(hours=6), 600, 25.5, 10.0, pv=500)
        today = battery.daily_health(rows, bulk_setpoint_v=28.2)[0]
        self.assertFalse(today["absorption_reached"])
        self.assertIsNone(today["absorption_minutes"])
        self.assertIsNone(today["tail_amps"])

    def test_local_day_boundaries_follow_the_offset(self):
        day = datetime(2026, 9, 5, 5, 0, tzinfo=timezone.utc)  # 22:00 the evening before at the site
        rows = readings(day, 60, 24.0, -2.0)
        self.assertEqual(battery.daily_health(rows, 28.2, utc_offset_minutes=-420)[0]["day"], "2026-09-04")
        self.assertEqual(battery.daily_health(rows, 28.2)[0]["day"], "2026-09-05")


if __name__ == "__main__":
    unittest.main()
