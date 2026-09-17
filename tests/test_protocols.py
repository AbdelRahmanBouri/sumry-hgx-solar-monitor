import unittest

from solar_monitor.protocols import create_protocol
from solar_monitor.protocols.base import Protocol
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol


class ProtocolRegistryTests(unittest.TestCase):
    def test_the_current_name_selects_the_protocol(self):
        protocol = create_protocol("sumry_8851")
        self.assertIsInstance(protocol, Sumry8851Protocol)
        self.assertIsInstance(protocol, Protocol)
        self.assertEqual(protocol.name, "sumry_8851")

    def test_the_legacy_name_still_works_and_reports_the_current_one(self):
        # The settings table and existing config files carry the old name.
        protocol = create_protocol("sumry_8851_probe")
        self.assertIsInstance(protocol, Sumry8851Protocol)
        self.assertEqual(protocol.name, "sumry_8851")

    def test_an_unknown_name_is_refused(self):
        with self.assertRaises(ValueError):
            create_protocol("sumry_9999")


if __name__ == "__main__":
    unittest.main()
