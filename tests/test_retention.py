from datetime import UTC, datetime
import sqlite3
import tempfile
import unittest

from solar_monitor.db import Database
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol
from solar_monitor.serial_io import Exchange
from tests.test_decode import CAPTURE_1
from tests.test_safety import REAL_CONFIG_REPLY

REFERENCE = datetime(2026, 9, 25, tzinfo=UTC)
OLD = "2026-09-10T00:00:00+00:00"     # 15 days before REFERENCE
RECENT = "2026-09-24T00:00:00+00:00"  # 1 day before
ANCIENT = "2026-05-01T00:00:00+00:00"  # past the 90-day window too


class RetentionTests(unittest.TestCase):
    """Routine frames age out after `success_days`, diagnostics after
    `diagnostic_days`. The frames here carry the decoder's own notes: a
    frame with a made-up note is what let the old rule pass its test while
    never deleting a single real frame."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")
        self.protocol = Sumry8851Protocol()

    def tearDown(self):
        self.temp.cleanup()

    def read(self, captured_at, response, *, query="status_candidate_0003", error=None):
        decoded = self.protocol.decode(response)
        exchange = Exchange(captured_at, "/dev/test", 9600, query, b"\x88\x51", response, 1, error)
        self.db.save_raw_frame(exchange.record(self.protocol.name, decoded.note, decoded.valid))

    def cleanup(self):
        return self.db.cleanup_retention(7, 90, reference_time=REFERENCE)["raw_frames_deleted"]

    def remaining(self):
        return sorted((row["captured_at"], row["query_name"]) for row in self.db.raw_frames(50))

    def test_decoded_frames_age_out_on_the_short_window(self):
        # Both real notes contain words the old substring rules treated as
        # trouble -- "operating_state/faults" and "not decoded".
        self.assertIn("not decoded", self.protocol.decode(CAPTURE_1).note)
        self.read(OLD, CAPTURE_1)
        self.read(OLD, REAL_CONFIG_REPLY, query="current_settings_0200")
        self.read(RECENT, CAPTURE_1)
        self.assertEqual(self.cleanup(), 2)
        self.assertEqual(self.remaining(), [(RECENT, "status_candidate_0003")])

    def test_failures_and_writes_are_kept_for_the_long_window(self):
        corrupted = CAPTURE_1[:-1] + bytes([CAPTURE_1[-1] ^ 0xFF])
        self.read(OLD, corrupted)
        self.read(OLD, b"", error="device reports readiness to read but returned no data")
        write = Exchange(OLD, "/dev/test", 9600, "write_settings_0200", REAL_CONFIG_REPLY, b"", 1)
        self.db.save_raw_frame(
            write.record(self.protocol.name, "configuration write: ['low_battery_cutoff_v']", decoded=False)
        )
        self.read(ANCIENT, corrupted)
        self.assertEqual(self.cleanup(), 1)
        self.assertEqual(
            [name for _, name in self.remaining()],
            ["status_candidate_0003", "status_candidate_0003", "write_settings_0200"],
        )

    def test_rows_from_before_the_flag_are_classified_by_their_note(self):
        legacy = {"port": "/dev/test", "baudrate": 9600, "protocol": "sumry_8851",
                  "request_hex": "", "response_hex": "", "response_ascii": "", "elapsed_ms": 1}
        rows = [
            ("status_candidate_0003", self.protocol.decode(CAPTURE_1).note, None),
            ("current_settings_0200", self.protocol.decode(REAL_CONFIG_REPLY).note, None),
            ("write_settings_0200", "configuration write: ['bulk_charge_voltage_v']", None),
            ("status_candidate_0003", "No reply bytes to decode", "I/O error"),
        ]
        for query, note, error in rows:
            self.db.save_raw_frame({**legacy, "captured_at": OLD, "query_name": query,
                                    "decode_note": note, "error": error})
        self.assertEqual(self.cleanup(), 2)
        self.assertEqual(
            [name for _, name in self.remaining()], ["status_candidate_0003", "write_settings_0200"]
        )

    def test_an_existing_database_gains_the_flag(self):
        path = f"{self.temp.name}/old.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE raw_frames (id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, "
                "port TEXT NOT NULL, baudrate INTEGER NOT NULL, protocol TEXT NOT NULL, "
                "query_name TEXT NOT NULL, request_hex TEXT NOT NULL, response_hex TEXT NOT NULL, "
                "response_ascii TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, decode_note TEXT, error TEXT)"
            )
        Database(path)
        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_frames)")}
        self.assertIn("decoded", columns)

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
