import unittest
from unittest.mock import patch

from solar_monitor import collector as collector_module
from solar_monitor.collector import Collector, start_collector
from solar_monitor.config import SerialConfig
from solar_monitor.serial_io import Exchange

from tests.test_safety import REAL_CONFIG_REPLY, as_reply


class FakeDatabase:
    def __init__(self):
        self.raw_frames = []
        self.samples = []
        self.configs = []

    def save_raw_frame(self, values):
        self.raw_frames.append(values)

    def save_sample(self, captured_at, metrics):
        self.samples.append((captured_at, metrics))

    def save_inverter_config(self, captured_at, settings):
        self.configs.append((captured_at, settings))


def _no_sleep(collector):
    """Make the poll interval instant so tests don't wait 30 real seconds."""
    collector.settings_event.wait = lambda timeout=None: collector.stop_event.is_set()


class CollectorResilienceTests(unittest.TestCase):
    """A single bad poll must never silently kill the background thread.

    Before this was guarded, any exception inside collect_once() escaped
    run_forever(), ending the daemon thread with no log line and no database
    row -- the poller just stopped forever and nothing anywhere said so.
    """

    def setUp(self):
        self.config = SerialConfig(port="auto", poll_seconds=30)
        self.database = FakeDatabase()

    def test_exception_in_cycle_does_not_kill_the_loop(self):
        collector = Collector(self.config, self.database)
        _no_sleep(collector)
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("serial gremlins")
            collector.stop()
            return True

        collector.collect_once = boom
        with self.assertLogs("solar_monitor.collector", level="INFO"):
            collector.run_forever()

        self.assertEqual(len(calls), 2, "loop should have continued after the exception")
        self.assertEqual(collector.failures, 1)
        self.assertIn("serial gremlins", collector.last_error)

    def test_missing_port_is_reported_not_raised(self):
        collector = Collector(self.config, self.database)
        collector._port = lambda: None
        with self.assertLogs("solar_monitor.collector", level="WARNING"):
            self.assertFalse(collector.collect_once())
        self.assertIn("No /dev/ttyUSB*", collector.last_error)
        self.assertEqual(self.database.raw_frames, [], "no row should be written with no port")

    def test_started_collector_reports_its_state(self):
        collector, thread = start_collector(self.config, self.database)
        _no_sleep(collector)
        collector.stop()
        thread.join(timeout=5)
        state = collector.state()
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(state["started_at"])
        self.assertEqual(state["interval_seconds"], 30)
        self.assertEqual(state["protocol"], "sumry_8851_probe")
        self.assertEqual(state["configured_port"], "auto")

    def test_new_collector_reports_not_running(self):
        state = Collector(self.config, self.database).state()
        self.assertFalse(state["running"])
        self.assertIsNone(state["started_at"])

    def test_apply_config_reads_writes_and_verifies_by_reading_back(self):
        collector = Collector(self.config, self.database)
        collector._port = lambda: "/dev/fake"
        sent = []
        replies = {"count": 0}

        def fake_exchange(port, baudrate, timeout, protocol, query, settle_seconds=0.15):
            replies["count"] += 1
            # First read: the inverter as it is. Second: as it is after the write.
            response = REAL_CONFIG_REPLY if replies["count"] == 1 else as_reply(sent[0])
            return Exchange("t", port, baudrate, query.name, query.frame, response, 5)

        def fake_write(port, baudrate, timeout, protocol, frame, template, settle_seconds=0.15):
            protocol.assert_write_frame(frame, template)
            sent.append(frame)
            return Exchange("t-write", port, baudrate, protocol.write_query_name, frame, b"", 5)

        with patch.object(collector_module, "exchange", fake_exchange), \
             patch.object(collector_module, "write_exchange", fake_write):
            result = collector.apply_config(
                {"bulk_charge_voltage_v": 29.2, "battery_type": "User-defined", "overload_auto_restart": True},
                settle_seconds=0,
            )

        self.assertEqual(len(sent), 1)
        self.assertTrue(result["verified"], result["mismatches"])
        self.assertEqual(result["before"]["bulk_charge_voltage_v"], 28.2)
        self.assertEqual(result["after"]["bulk_charge_voltage_v"], 29.2)
        self.assertEqual(result["after"]["battery_type"], "User-defined")
        self.assertTrue(result["after"]["overload_auto_restart"])
        # Both reads and the write are on record, and the read-back is a snapshot.
        self.assertEqual([f["query_name"] for f in self.database.raw_frames],
                         ["current_settings_0200", "write_settings_0200", "current_settings_0200"])
        self.assertEqual(len(self.database.configs), 2)

    def test_apply_config_is_not_verified_when_the_inverter_ignores_it(self):
        collector = Collector(self.config, self.database)
        collector._port = lambda: "/dev/fake"

        def stubborn(port, baudrate, timeout, protocol, query, settle_seconds=0.15):
            return Exchange("t", port, baudrate, query.name, query.frame, REAL_CONFIG_REPLY, 5)

        def fake_write(port, baudrate, timeout, protocol, frame, template, settle_seconds=0.15):
            return Exchange("t-write", port, baudrate, protocol.write_query_name, frame, b"", 5)

        with patch.object(collector_module, "exchange", stubborn), \
             patch.object(collector_module, "write_exchange", fake_write):
            result = collector.apply_config({"float_charge_voltage_v": 27.2}, settle_seconds=0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["mismatches"]["float_charge_voltage_v"]["inverter"], 27.0)

    def test_apply_config_refuses_without_a_readable_configuration(self):
        collector = Collector(self.config, self.database)
        collector._port = lambda: None
        with self.assertRaises(ValueError):
            collector.apply_config({"buzzer": False})
        collector._port = lambda: "/dev/fake"

        def broken(port, baudrate, timeout, protocol, query, settle_seconds=0.15):
            return Exchange("t", port, baudrate, query.name, query.frame, b"", 5, "port vanished")

        writes = []
        with patch.object(collector_module, "exchange", broken), \
             patch.object(collector_module, "write_exchange", lambda *a, **k: writes.append(a)):
            with self.assertRaises(ValueError):
                collector.apply_config({"buzzer": False}, settle_seconds=0)
        self.assertEqual(writes, [], "nothing may be written without a valid template")

    def test_poll_interval_can_change_at_runtime(self):
        collector = Collector(self.config, self.database)
        collector.set_poll_seconds(10)
        self.assertEqual(collector.state()["interval_seconds"], 10)
        self.assertTrue(collector.settings_event.is_set())
        with self.assertRaises(ValueError):
            collector.set_poll_seconds(5)


if __name__ == "__main__":
    unittest.main()
