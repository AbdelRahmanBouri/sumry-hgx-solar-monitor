from datetime import datetime, timezone
import tempfile
import unittest

from solar_monitor.db import Database


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def frame(self, captured_at, *, note="Decoded status frame", error=None):
        self.db.save_raw_frame({
            "captured_at": captured_at, "port": "/dev/test", "baudrate": 9600,
            "protocol": "test", "query_name": "status", "request_hex": "00",
            "response_hex": "01", "response_ascii": ".", "elapsed_ms": 1,
            "decode_note": note, "error": error,
        })

    def test_routine_frames_expire_before_diagnostics(self):
        self.frame("2026-08-01T00:00:00+00:00")
        self.frame("2026-08-01T00:01:00+00:00", error="timeout")
        self.frame("2026-05-01T00:00:00+00:00", note="CRC invalid")
        self.frame("2026-09-02T00:00:00+00:00")
        result = self.db.cleanup_retention(
            7, 90, reference_time=datetime(2026, 9, 3, tzinfo=timezone.utc)
        )
        rows = self.db.raw_frames(10)
        self.assertEqual(result["raw_frames_deleted"], 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row["error"] == "timeout" for row in rows))

    def test_history_downsampling_keeps_edges_and_limit(self):
        for minute in range(100):
            self.db.save_sample(
                f"2026-09-03T00:{minute // 60:02d}:{minute % 60:02d}+00:00",
                {"pv_watts": minute},
            )
        rows = self.db.history("2026-09-03T00:00:00+00:00", max_points=20)
        self.assertLessEqual(len(rows), 20)
        self.assertEqual(rows[0]["pv_watts"], 0)
        self.assertEqual(rows[-1]["pv_watts"], 99)


if __name__ == "__main__":
    unittest.main()
