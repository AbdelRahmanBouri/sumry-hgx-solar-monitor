from pathlib import Path
import tempfile
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
        self.assertEqual(state["protocol"], "sumry_8851")
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
                {"bulk_charge_voltage_v": 29.2, "overload_auto_restart": True},
                settle_seconds=0,
            )

        self.assertEqual(len(sent), 1)
        self.assertTrue(result["verified"], result["mismatches"])
        self.assertEqual(result["before"]["bulk_charge_voltage_v"], 28.2)
        self.assertEqual(result["after"]["bulk_charge_voltage_v"], 29.2)
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


class FailureVisibilityTests(unittest.TestCase):
    """A dead serial adapter must be visible to anything watching.

    On 2026-09-05 the adapter stalled for 425 consecutive polls and the
    collector still reported failures=0, because only unhandled exceptions
    were counted. Nothing could ever have alarmed on it.
    """

    def setUp(self):
        self.database = FakeDatabase()
        self.collector = Collector(SerialConfig(port="auto"), self.database)
        self.collector._port = lambda: "/dev/fake"

    def _failing(self, *_a, **_k):
        return Exchange("2026-09-05T12:00:00+00:00", "/dev/fake", 9600, "q", b"", b"", 5, "[Errno 5] I/O error")

    def test_failed_exchanges_are_counted_and_streaked(self):
        with patch.object(collector_module, "exchange", self._failing):
            for _ in range(3):
                self.collector.collect_once()
        state = self.collector.state()
        self.assertGreaterEqual(state["failures"], 3)
        self.assertGreaterEqual(state["consecutive_failures"], 3)
        self.assertEqual(state["failing_since"], "2026-09-05T12:00:00+00:00")

    def test_a_success_clears_the_streak(self):
        from tests.test_safety import REAL_CONFIG_REPLY
        with patch.object(collector_module, "exchange", self._failing):
            self.collector.collect_once()
        # The first poll runs two queries (status and config), so two failures.
        self.assertGreater(self.collector.state()["consecutive_failures"], 0)

        def ok(*_a, **_k):
            return Exchange("2026-09-05T12:01:00+00:00", "/dev/fake", 9600, "q", b"", REAL_CONFIG_REPLY, 5)

        with patch.object(collector_module, "exchange", ok):
            self.collector.collect_once()
        self.assertEqual(self.collector.state()["consecutive_failures"], 0)
        self.assertIsNone(self.collector.state()["failing_since"])

    def test_last_success_age_counts_from_the_last_answer(self):
        from tests.test_safety import REAL_CONFIG_REPLY
        self.assertIsNone(self.collector.state()["last_success_age_seconds"])
        with patch.object(collector_module, "exchange", self._failing):
            self.collector.collect_once()
        self.assertIsNone(self.collector.state()["last_success_age_seconds"],
                          "a link that has never answered has no age to report")

        def ok(*_a, **_k):
            return Exchange("2026-09-05T12:01:00+00:00", "/dev/fake", 9600, "q", b"", REAL_CONFIG_REPLY, 5)

        with patch.object(collector_module.time, "monotonic", return_value=1000.0), \
             patch.object(collector_module, "exchange", ok):
            self.collector.collect_once()
        with patch.object(collector_module.time, "monotonic", return_value=1000.5):
            self.assertAlmostEqual(self.collector.state()["last_success_age_seconds"], 0.5)

        # Later failures do not touch it: the age keeps growing from the
        # last answer, which is what a watchdog needs to see.
        with patch.object(collector_module.time, "monotonic", return_value=1400.0), \
             patch.object(collector_module, "exchange", self._failing):
            self.collector.collect_once()
            self.assertAlmostEqual(self.collector.state()["last_success_age_seconds"], 400.0)


class ClockGuardTests(unittest.TestCase):
    """A Pi has no RTC: after a cold boot it restores a stale clock until NTP
    syncs. That put two real readings ~10 hours in the past on 2026-09-05,
    showing 599 W of sun at midnight."""

    def setUp(self):
        self.database = FakeDatabase()
        self.collector = Collector(SerialConfig(port="auto"), self.database)
        self.collector._port = lambda: "/dev/fake"

    def test_a_reading_stamped_before_one_already_stored_is_discarded(self):
        from tests.test_safety import REAL_CONFIG_REPLY
        self.collector._newest_stored = "2026-09-05T18:00:00+00:00"

        def stale(*_a, **_k):
            return Exchange("2026-09-05T07:18:27+00:00", "/dev/fake", 9600,
                            "current_settings_0200", b"", REAL_CONFIG_REPLY, 5)

        with patch.object(collector_module, "_SYNC_FLAG", Path("/nonexistent/flag")), \
             patch.object(collector_module, "exchange", stale):
            self.collector.collect_once()
        self.assertEqual(self.database.configs, [], "a backwards-stamped reading must not be stored")
        self.assertTrue(self.database.raw_frames, "the raw frame is still kept for diagnosis")

    def unsynced_poll(self, collector):
        """A poll while timesyncd runs but has not synced: its state
        directory exists, the synchronized flag does not."""
        from tests.test_safety import REAL_CONFIG_REPLY

        def reply(*_a, **_k):
            return Exchange("2026-09-05T18:00:00+00:00", "/dev/fake", 9600,
                            "current_settings_0200", b"", REAL_CONFIG_REPLY, 5)

        with tempfile.TemporaryDirectory() as run_dir:
            collector._newest_stored = "2026-09-05T07:00:00+00:00"
            with patch.object(collector_module, "_SYNC_FLAG", Path(run_dir) / "synchronized"), \
                 patch.object(collector_module, "exchange", reply):
                collector.collect_once()

    def test_an_unsynced_clock_holds_readings_back_by_default(self):
        self.unsynced_poll(self.collector)
        self.assertEqual(self.database.configs, [])

    def test_a_pi_with_an_rtc_can_trust_its_clock_without_a_sync(self):
        """With an RTC but no internet, timesyncd never syncs; requiring it
        would discard every reading for good."""
        collector = Collector(SerialConfig(port="auto", require_clock_sync=False), self.database)
        collector._port = lambda: "/dev/fake"
        self.unsynced_poll(collector)
        self.assertEqual(len(self.database.configs), 1)

    def test_a_forward_reading_is_stored_normally(self):
        from tests.test_safety import REAL_CONFIG_REPLY
        self.collector._newest_stored = "2026-09-05T07:00:00+00:00"

        def fresh(*_a, **_k):
            return Exchange("2026-09-05T18:00:00+00:00", "/dev/fake", 9600,
                            "current_settings_0200", b"", REAL_CONFIG_REPLY, 5)

        with patch.object(collector_module, "_SYNC_FLAG", Path("/nonexistent/flag")), \
             patch.object(collector_module, "exchange", fresh):
            self.collector.collect_once()
        self.assertEqual(len(self.database.configs), 1)
