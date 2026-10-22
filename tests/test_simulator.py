from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from solar_monitor import battery, simulator
from solar_monitor.collector import Collector
from solar_monitor.config import SerialConfig
from solar_monitor.db import Database
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol
from solar_monitor.serial_io import exchange

NOON = datetime(2026, 6, 21, 19, 0, tzinfo=UTC)  # solar noon on the simulated site's meridian
MIDNIGHT = NOON + timedelta(hours=12)


def site():
    # The demo site's longitude follows the machine's time zone; tests pin
    # it so noon is noon wherever they run.
    return replace(simulator.demo_site(), longitude=-105.0)


class SimulatedInverterTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Sumry8851Protocol()
        self.inverter = simulator.SimulatedInverter(site(), start=NOON - timedelta(hours=6))

    def decode(self, at):
        result = self.protocol.decode(self.inverter.respond(self.protocol.queries[0].frame, at))
        self.assertTrue(result.valid, result.note)
        return result.metrics

    def test_incomplete_or_nonfinite_site_is_rejected_before_simulation(self):
        for name in ('latitude', 'longitude', 'tilt_degrees', 'azimuth_degrees',
                     'array_capacity_watts', 'vmp_volts', 'voc_volts'):
            for value in (None, float('nan'), float('inf')):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    simulator.SimulatedInverter(replace(site(), **{name: value}))
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(capacity=value), self.assertRaises(ValueError):
                simulator.SimulatedInverter(site(), capacity_ah=value)
        with self.assertRaises(ValueError):
            simulator.SimulatedInverter(replace(site(), array_capacity_watts=0))

    def test_frames_pass_the_decoders_own_checks(self):
        metrics = self.decode(NOON)
        self.assertEqual(metrics["output_voltage"], round(metrics["output_voltage"], 1))
        config = self.protocol.decode(self.inverter.respond(self.protocol.queries[1].frame))
        self.assertTrue(config.valid, config.note)
        self.assertEqual(config.metrics["bulk_charge_voltage_v"], 28.8)

    def test_the_sun_shines_at_noon_and_not_at_midnight(self):
        noon = self.decode(NOON)
        # A full bank curtails the panels to the load, so the sun is shown by
        # carrying the house, not by any fixed wattage.
        self.assertGreater(noon["pv_watts"], 300)
        self.assertGreater(noon["pv_watts"], noon["ac_load_watts"])
        midnight = self.decode(MIDNIGHT)
        self.assertEqual(midnight["pv_watts"], 0)
        self.assertLess(midnight["battery_current"], 0, "the bank carries the night")
        for reading in (noon, midnight):
            self.assertTrue(22.0 < reading["battery_voltage"] < 29.5, reading["battery_voltage"])
            self.assertGreater(reading["ac_load_watts"], 0)

    def test_only_the_allow_listed_queries_are_answered(self):
        self.assertEqual(self.inverter.respond(bytes.fromhex("88 51 00 04 00 00 00 00")), b"")

    def test_a_gated_write_lands_and_a_forged_one_does_not(self):
        current = self.inverter.respond(self.protocol.queries[1].frame)
        write = self.protocol.build_config_write(current, {"float_charge_voltage_v": 27.0})
        self.assertTrue(self.inverter.respond(write))
        self.assertEqual(self.inverter.settings()["float_charge_voltage_v"], 27.0)
        self.assertEqual(self.inverter.float_volts, 27.0, "the charger follows its new setpoint")

        forged = bytearray(self.protocol.build_config_write(
            self.inverter.respond(self.protocol.queries[1].frame), {"buzzer": False}
        ))
        forged[14] ^= 0x01  # output voltage: not writable
        self.assertEqual(self.inverter.respond(bytes(forged)), b"")
        self.assertEqual(self.inverter.settings()["output_voltage_v"], 120.0)


class SimulatorIntegrationTests(unittest.TestCase):
    """The simulator is only worth having if the real code paths run
    against it unchanged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(f"{self.temp.name}/demo.sqlite3")
        simulator.install(simulator.SimulatedInverter(site()))
        self.addCleanup(simulator.install, None)

    def test_the_serial_transport_reaches_it_through_pyserial(self):
        protocol = Sumry8851Protocol()
        result = exchange("sim://", 9600, 1.0, protocol, protocol.queries[0], settle_seconds=0)
        self.assertIsNone(result.error)
        self.assertTrue(protocol.decode(result.response).valid)

    def test_the_collector_polls_it_and_applies_a_write(self):
        collector = Collector(SerialConfig(port="sim://"), self.database)
        self.assertTrue(collector.collect_once())
        self.assertIsNotNone(self.database.latest_sample())
        self.database.set_settings({"writes_enabled": "true"}, "now")
        outcome = collector.apply_config({"charge_cutoff_current_a": 5}, settle_seconds=0)
        self.assertTrue(outcome["verified"], outcome["mismatches"])

    def test_a_backfilled_week_gives_the_charge_counter_its_anchors(self):
        inverter = simulator.SimulatedInverter(site())
        count = simulator.backfill(self.database, inverter, days=4, until=NOON + timedelta(days=4))
        self.assertEqual(count, 4 * 2880)
        rows = self.database.history((NOON - timedelta(days=1)).isoformat())
        state = battery.count_charge(rows, 200, bulk_setpoint_v=28.8)
        self.assertEqual(state["method"], "coulomb")
        self.assertTrue(state["full_events"] and state["rest_events"])
        # The bank holds less than its nameplate, and the counter finds out.
        self.assertEqual(state["capacity_source"], "measured")
        self.assertLess(state["capacity_ah"], 195)
        self.assertIsNotNone(self.database.latest_inverter_config())


class DemoTests(unittest.TestCase):
    def test_prepare_demo_builds_a_self_contained_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            config, database = simulator.prepare_demo(Path(temp) / "demo", days=1)
            self.addCleanup(simulator.install, None)
            self.addCleanup(simulator.solar_estimate.use_weather, None)
            self.assertEqual(config.serial.port, "sim://")
            self.assertEqual(config.web.host, "127.0.0.1")
            self.assertEqual(database.admin_count(), 1)
            self.assertGreater(database.storage_counts()["samples"], 2000)
            self.assertIsNotNone(config.site.latitude)


if __name__ == "__main__":
    unittest.main()
