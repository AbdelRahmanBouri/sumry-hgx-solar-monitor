import unittest

from solar_monitor.protocols.sumry_8851 import Sumry8851ProbeProtocol
from solar_monitor.serial_io import escaped_ascii


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Sumry8851ProbeProtocol()

    def test_candidate_query_is_exact(self):
        query = self.protocol.query("status_candidate_0003")
        self.assertEqual(query.frame.hex(" ").upper(), "88 51 00 03 00 00 00 00 4D 08")
        self.protocol.assert_read_only_frame(query.frame)

    def test_config_query_is_exact_and_read_only(self):
        query = self.protocol.query("current_settings_0200")
        self.assertEqual(query.frame.hex(" ").upper(), "88 51 00 03 02 00 00 00 4C B0")
        self.protocol.assert_read_only_frame(query.frame)

    def test_unknown_frame_is_rejected(self):
        with self.assertRaises(PermissionError):
            self.protocol.assert_read_only_frame(bytes.fromhex("88 51 00 04 00 00 00 00"))

    def test_ascii_rendering(self):
        self.assertEqual(escaped_ascii(b"OK\x00\xff"), "OK..")


if __name__ == "__main__":
    unittest.main()
