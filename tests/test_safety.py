import struct
import unittest

from solar_monitor.protocols.sumry_8851 import Sumry8851ProbeProtocol, _crc16_modbus
from solar_monitor.serial_io import escaped_ascii, write_exchange

# A real configuration reply captured from the HGX on 2026-09-04: AGM preset,
# 28.2 V bulk, 27.0 V float, 5 A cut-off, 21.0 V low cut-off, 80 A charge.
REAL_CONFIG_REPLY = bytes.fromhex(
    "88 51 00 03 02 00 5E 00 80 A4 AF C4 A0 0F B0 04 70 17 00 00 00 00 48 0D A0 0F 48 0D 78 05"
    " 8C 0A B6 03 1C 0C B8 0B 34 08 98 08 78 05 A0 0F 74 0E 04 0B F0 0A 8C 0A 60 09 C8 00 20 03"
    " 32 00 00 00 00 00 3C FB 32 00 3C EC 32 F6 AC 0D AC 0D F4 01 E4 0C 00 00 55 4B 50 50 46 50"
    " 68 0B 3C 00 78 00 1E 00 32 5F 14 00 5C 39"
)


def as_reply(write_frame: bytes) -> bytes:
    """What the inverter reports after accepting a write: the same block with
    the read command and a fresh checksum."""
    frame = bytearray(write_frame)
    frame[2:4] = bytes.fromhex("0003")
    struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
    return bytes(frame)


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


class ConfigurationWriteTests(unittest.TestCase):
    """A write is the inverter's own reply sent back with the command changed
    and only whitelisted fields altered. These pin that property down."""

    def setUp(self):
        self.protocol = Sumry8851ProbeProtocol()

    def test_the_captured_reply_decodes_to_the_known_settings(self):
        decoded = self.protocol.decode(REAL_CONFIG_REPLY)
        self.assertTrue(decoded.valid, decoded.note)
        self.assertEqual(decoded.metrics["bulk_charge_voltage_v"], 28.2)
        self.assertEqual(decoded.metrics["float_charge_voltage_v"], 27.0)
        self.assertEqual(decoded.metrics["battery_type"], "AGM")
        self.assertFalse(decoded.metrics["overload_auto_restart"])

    def test_write_frame_changes_only_the_requested_bytes(self):
        frame = self.protocol.build_config_write(REAL_CONFIG_REPLY, {"bulk_charge_voltage_v": 29.2})
        self.assertEqual(len(frame), len(REAL_CONFIG_REPLY))
        self.assertEqual(frame[:2], bytes.fromhex("8851"))
        self.assertEqual(frame[2:4], bytes.fromhex("0010"), "command must be write")
        self.assertEqual(frame[4:8], REAL_CONFIG_REPLY[4:8], "address and length unchanged")
        self.assertEqual(struct.unpack_from("<H", frame, 48)[0], 2920)
        self.assertEqual(int.from_bytes(frame[-2:], "little"), _crc16_modbus(frame[:-2]))
        # 28.20 V is 0x0B04 and 29.20 V is 0x0B68: only the low byte moves.
        differing = [i for i in range(8, len(frame) - 2) if frame[i] != REAL_CONFIG_REPLY[i]]
        self.assertEqual(differing, [48])

    def test_the_inverter_would_read_the_new_values_back(self):
        changes = {
            "battery_type": "User-defined", "bulk_charge_voltage_v": 29.2,
            "float_charge_voltage_v": 27.2, "charge_cutoff_current_a": 2,
            "low_battery_cutoff_v": 22.0, "overload_auto_restart": True,
            "equalization_enabled": 1, "equalization_voltage_v": 29.5,
        }
        frame = self.protocol.build_config_write(REAL_CONFIG_REPLY, changes)
        decoded = self.protocol.decode(as_reply(frame))
        self.assertTrue(decoded.valid, decoded.note)
        self.assertEqual(decoded.metrics["battery_type"], "User-defined")
        self.assertEqual(decoded.metrics["bulk_charge_voltage_v"], 29.2)
        self.assertEqual(decoded.metrics["float_charge_voltage_v"], 27.2)
        self.assertEqual(decoded.metrics["charge_cutoff_current_a"], 2.0)
        self.assertEqual(decoded.metrics["low_battery_cutoff_v"], 22.0)
        self.assertTrue(decoded.metrics["overload_auto_restart"])
        self.assertTrue(decoded.metrics["equalization_enabled"])
        self.assertEqual(decoded.metrics["equalization_voltage_v"], 29.5)
        # Bits sharing a flag byte with a changed one must survive untouched.
        self.assertEqual(decoded.metrics["output_frequency_mode"], "60 Hz")
        self.assertEqual(decoded.metrics["ac_input_range"], "UPS")
        self.assertTrue(decoded.metrics["overtemperature_auto_restart"])
        self.assertTrue(decoded.metrics["fault_recording"])

    def test_values_the_menu_would_refuse_are_refused_here(self):
        for changes in (
            {"bulk_charge_voltage_v": 29.3}, {"bulk_charge_voltage_v": 23.9},
            {"low_battery_cutoff_v": 19.5}, {"charge_cutoff_current_a": 1},
            {"equalization_voltage_v": 30}, {"battery_type": "Lithium"},
            {"output_voltage_v": 110}, {"inverter_power_limit_w": 6000},
            {"bulk_charge_voltage_v": "high"}, {},
        ):
            with self.assertRaises(ValueError, msg=str(changes)):
                self.protocol.build_config_write(REAL_CONFIG_REPLY, changes)

    def test_a_write_cannot_be_built_from_anything_but_a_valid_reply(self):
        corrupted = bytearray(REAL_CONFIG_REPLY)
        corrupted[20] ^= 0xFF
        with self.assertRaises(ValueError):
            self.protocol.build_config_write(bytes(corrupted), {"buzzer": False})
        with self.assertRaises(ValueError):
            self.protocol.build_config_write(REAL_CONFIG_REPLY[:100], {"buzzer": False})

    def test_the_gate_rejects_a_frame_that_touches_anything_else(self):
        frame = bytearray(self.protocol.build_config_write(REAL_CONFIG_REPLY, {"buzzer": False}))
        frame[14] = 0x00  # output voltage: not writable
        struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
        with self.assertRaises(PermissionError):
            self.protocol.assert_write_frame(bytes(frame), REAL_CONFIG_REPLY)
        # ...and a flag bit outside the whitelist in an otherwise writable byte.
        frame = bytearray(self.protocol.build_config_write(REAL_CONFIG_REPLY, {"buzzer": False}))
        frame[10] ^= 0x10  # power saving
        struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
        with self.assertRaises(PermissionError):
            self.protocol.assert_write_frame(bytes(frame), REAL_CONFIG_REPLY)

    def test_the_gate_rejects_bad_checksums_and_read_commands(self):
        frame = bytearray(self.protocol.build_config_write(REAL_CONFIG_REPLY, {"buzzer": False}))
        frame[-1] ^= 0x01
        with self.assertRaises(PermissionError):
            self.protocol.assert_write_frame(bytes(frame), REAL_CONFIG_REPLY)
        with self.assertRaises(PermissionError):
            self.protocol.assert_write_frame(REAL_CONFIG_REPLY, REAL_CONFIG_REPLY)

    def test_write_exchange_refuses_before_touching_the_port(self):
        with self.assertRaises(PermissionError):
            write_exchange("/dev/does-not-exist", 9600, 1, self.protocol, REAL_CONFIG_REPLY, REAL_CONFIG_REPLY)
        with self.assertRaises(PermissionError):
            self.protocol.assert_read_only_frame(
                self.protocol.build_config_write(REAL_CONFIG_REPLY, {"buzzer": False})
            )


if __name__ == "__main__":
    unittest.main()
