import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest

from solar_monitor.db import Database
from solar_monitor.protocols.sumry_8851 import _crc16_modbus

from tests.test_safety import REAL_CONFIG_REPLY

TOOL = Path(__file__).resolve().parent.parent / "tools" / "discover_registers.py"
spec = importlib.util.spec_from_file_location("discover_registers", TOOL)
discover_registers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discover_registers)


def with_word(frame: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(frame)
    struct.pack_into("<H", changed, offset, value)
    struct.pack_into("<H", changed, len(changed) - 2, _crc16_modbus(bytes(changed[:-2])))
    return bytes(changed)


class DiscoverRegistersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/test.sqlite3"
        self.db = Database(self.database)
        self.config = Path(self.temp.name) / "config.toml"
        self.config.write_text(f'[storage]\ndatabase = "{self.database}"\n')

    def save(self, captured_at, frame):
        self.db.save_raw_frame({
            "captured_at": captured_at, "port": "/dev/test", "baudrate": 9600, "protocol": "sumry_8851",
            "query_name": "current_settings_0200", "request_hex": "", "response_hex": frame.hex(" ").upper(),
            "response_ascii": "", "elapsed_ms": 1, "decode_note": "CRC-validated", "error": None, "decoded": 1,
        })

    def run_tool(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = discover_registers.main(["--config", str(self.config), *args])
        return status, output.getvalue()

    def test_the_baseline_survives_its_frame_ageing_out(self):
        self.save("2026-09-10T00:00:00+00:00", REAL_CONFIG_REPLY)
        status, _ = self.run_tool("--mark")
        self.assertEqual(status, 0)

        # Retention removes the baseline's own frame; a later poll shows the
        # panel change. The diff must still be against the baseline.
        with self.db.connect() as connection:
            connection.execute("DELETE FROM raw_frames")
        self.save("2026-09-18T00:00:00+00:00", with_word(REAL_CONFIG_REPLY, 82, 4))
        status, output = self.run_tool("--diff")
        self.assertEqual(status, 0)
        self.assertIn("offset  82", output)
        self.assertIn("0 ->      4", output)

    def test_a_timestamp_only_mark_still_works(self):
        self.save("2026-09-10T00:00:00+00:00", REAL_CONFIG_REPLY)
        self.save("2026-09-10T00:05:00+00:00", with_word(REAL_CONFIG_REPLY, 82, 4))
        discover_registers.mark_file(self.database).write_text("2026-09-10T00:00:00+00:00")
        status, output = self.run_tool("--diff")
        self.assertEqual(status, 0)
        self.assertIn("offset  82", output)

    def test_no_poll_since_the_baseline_says_so(self):
        self.save("2026-09-10T00:00:00+00:00", REAL_CONFIG_REPLY)
        self.run_tool("--mark")
        status, output = self.run_tool("--diff")
        self.assertEqual(status, 1)
        self.assertIn("wait for one", output)


if __name__ == "__main__":
    unittest.main()
