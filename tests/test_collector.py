import unittest

from solar_monitor.collector import Collector, start_collector
from solar_monitor.config import SerialConfig


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
    collector.stop_event.wait = lambda timeout=None: collector.stop_event.is_set()


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


if __name__ == "__main__":
    unittest.main()
