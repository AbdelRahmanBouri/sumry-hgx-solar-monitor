import unittest

from solar_monitor import utility

# Real readings from this installation. The overnight pair is the case the
# whole module exists for: utility present, inverter output collapsed to
# nothing, battery flat, and ~400 W of load still being served -- which only
# mains can be doing.
BYPASS_AT_NIGHT = {
    "grid_voltage": 121.6, "grid_frequency": 59.97,
    "output_voltage": 121.2, "output_current": 0.08,
    "ac_load_watts": 370.0, "pv_watts": 0.0,
    "battery_voltage": 25.72, "battery_current": 0.10,
}
# Same hour the night before the utility was wired in: no mains, and the
# battery is visibly carrying the load.
BATTERY_AT_NIGHT = {
    "grid_voltage": 0.0, "grid_frequency": 0.0,
    "output_voltage": 120.0, "output_current": 1.95,
    "ac_load_watts": 185.0, "pv_watts": 11.0,
    "battery_voltage": 25.01, "battery_current": -8.60,
}
# Midday: mains present and idle while the panels run the house.
SOLAR_AT_MIDDAY = {
    "grid_voltage": 123.6, "grid_frequency": 60.03,
    "output_voltage": 120.4, "output_current": 3.63,
    "ac_load_watts": 401.0, "pv_watts": 552.0,
    "battery_voltage": 26.98, "battery_current": 2.40,
}


class UtilityStateTests(unittest.TestCase):
    def test_bypass_is_recognised_as_carrying_the_load(self):
        state = utility.utility_state(BYPASS_AT_NIGHT)
        self.assertTrue(state["present"])
        self.assertTrue(state["carrying_load"])
        self.assertEqual(state["state"], "carrying")
        # load + a trickle into the battery, with no PV to offset it.
        self.assertAlmostEqual(state["watts"], 372.6, places=1)
        # ...and that power over the measured utility voltage.
        self.assertAlmostEqual(state["amps"], 3.06, places=2)

    def test_absent_utility_reports_no_draw(self):
        state = utility.utility_state(BATTERY_AT_NIGHT)
        self.assertFalse(state["present"])
        self.assertFalse(state["carrying_load"])
        self.assertEqual(state["state"], "absent")
        self.assertEqual(state["watts"], 0.0)
        self.assertEqual(state["amps"], 0.0)

    def test_present_but_idle_while_solar_runs_the_house(self):
        state = utility.utility_state(SOLAR_AT_MIDDAY)
        self.assertTrue(state["present"])
        self.assertFalse(state["carrying_load"])
        self.assertEqual(state["state"], "standby")
        # PV covers the load and the charge, so the balance must not go
        # negative and imply the utility is absorbing power.
        self.assertEqual(state["watts"], 0.0)

    def test_small_load_is_not_attributed_either_way(self):
        state = utility.utility_state({**BYPASS_AT_NIGHT, "ac_load_watts": 18.0})
        self.assertTrue(state["present"])
        self.assertIsNone(state["carrying_load"])
        self.assertEqual(state["state"], "unknown")

    def test_missing_fields_never_fabricate_a_verdict(self):
        state = utility.utility_state({"grid_voltage": 121.6})
        self.assertTrue(state["present"])
        self.assertIsNone(state["carrying_load"])
        self.assertIsNone(state["watts"])

        empty = utility.utility_state(None)
        self.assertFalse(empty["present"])
        self.assertEqual(empty["state"], "absent")

    def test_amps_are_never_invented_without_the_whole_balance(self):
        # No PV reading means the balance cannot be closed, so there is no
        # honest current to report -- rather than dividing a guess by volts.
        state = utility.utility_state({k: v for k, v in BYPASS_AT_NIGHT.items() if k != "pv_watts"})
        self.assertTrue(state["present"])
        self.assertIsNone(state["watts"])
        self.assertIsNone(state["amps"])

    def test_non_numeric_readings_are_rejected(self):
        state = utility.utility_state({"grid_voltage": "n/a", "ac_load_watts": None})
        self.assertFalse(state["present"])
        self.assertEqual(state["state"], "absent")

    def test_brownout_below_the_present_threshold_counts_as_absent(self):
        state = utility.utility_state({**BYPASS_AT_NIGHT, "grid_voltage": 42.0})
        self.assertFalse(state["present"])
        self.assertEqual(state["state"], "absent")


if __name__ == "__main__":
    unittest.main()
