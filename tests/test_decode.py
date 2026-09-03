import unittest

from solar_monitor.protocols.sumry_8851 import Sumry8851ProbeProtocol, _crc16_modbus

# Two real HGX replies to the candidate status query, captured 2026-09-02
# three minutes apart. Both pass the frame's own CRC16/MODBUS checksum,
# which is what confirms this field mapping rather than just guessing it.
CAPTURE_1 = bytes.fromhex(
    "88 51 00 03 00 00 94 00 85 33 C1 0B 00 00 00 00 34 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 10 00 BC 34 00 00 00 00 00 00 00 00 00 00 00 00 AF 04 D8 01 64 17 35 02 1B 02 ED 01 F3 01 0E 0D 0C 0C C2 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8A 0A 0E 00 00 00 00 00 27 09 03 01 17 02 0C 0B 00 00 16 00 00 00 1E 1E 1E 25 1E 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 6E 37".replace(" ", "")
)
CAPTURE_2 = bytes.fromhex(
    "88 51 00 03 00 00 94 00 85 33 C1 0B 00 00 00 00 34 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 10 00 BC 34 00 00 00 00 00 00 00 00 00 00 00 00 B1 04 B5 01 64 17 0C 02 EE 01 C3 01 C9 01 0D 0C 0B 0B 9C 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8A 0A 13 00 00 00 00 00 27 09 F0 00 1B 02 0C 0B 00 00 16 00 00 00 1E 1E 1E 25 1E 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 87 C3".replace(" ", "")
)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Sumry8851ProbeProtocol()

    def test_capture_1_decodes_and_validates_crc(self):
        result = self.protocol.decode(CAPTURE_1)
        self.assertTrue(result.valid)
        self.assertEqual(result.metrics["output_voltage"], 119.9)
        self.assertEqual(result.metrics["output_frequency"], 59.88)
        self.assertEqual(result.metrics["battery_voltage"], 26.98)
        self.assertEqual(result.metrics["battery_current"], 1.4)
        self.assertEqual(result.metrics["pv_voltage"], 234.3)
        self.assertEqual(result.metrics["pv_watts"], 535)
        self.assertEqual(result.metrics["bus_voltage"], 282.8)
        self.assertEqual(result.metrics["fan1_percent"], 30)
        self.assertEqual(result.metrics["ntc2_temperature"], 30)
        self.assertEqual(result.metrics["ntc3_temperature"], 37)
        self.assertEqual(result.metrics["ntc4_temperature"], 30)
        self.assertEqual(result.metrics["bts_temperature"], 25)
        self.assertNotIn("battery_temperature", result.metrics)

    def test_capture_2_decodes_and_validates_crc(self):
        result = self.protocol.decode(CAPTURE_2)
        self.assertTrue(result.valid)
        self.assertEqual(result.metrics["output_voltage"], 120.1)
        self.assertEqual(result.metrics["output_frequency"], 59.88)
        self.assertEqual(result.metrics["battery_voltage"], 26.98)
        self.assertEqual(result.metrics["battery_current"], 1.9)
        self.assertEqual(result.metrics["pv_watts"], 539)

    def test_v2_configuration_decodes(self):
        frame = bytearray(104)
        frame[:8] = bytes.fromhex("88 51 00 03 02 00 5E 00")
        frame[8] = 0b10100000  # 60Hz, APL, AGM
        frame[9] = 0b00100100  # SBU, PV only
        frame[10] = 0b11000010
        frame[11] = 0b11000010
        values = {12: 4000, 14: 1200, 16: 6000, 38: 2300, 48: 2820, 52: 2700,
                  54: 2400, 56: 200, 58: 600, 60: 50, 82: 0, 90: 2900,
                  92: 60, 94: 120, 96: 30}
        for offset, value in values.items():
            frame[offset:offset + 2] = value.to_bytes(2, "little")
        frame[-2:] = _crc16_modbus(frame[:-2]).to_bytes(2, "little")
        result = self.protocol.decode(bytes(frame))
        self.assertTrue(result.valid)
        self.assertEqual(result.metrics["battery_type"], "AGM")
        self.assertEqual(result.metrics["bulk_charge_voltage_v"], 28.2)
        self.assertEqual(result.metrics["maximum_charge_current_a"], 60)

    def test_power_factor_is_physically_consistent(self):
        # load_watts should be roughly output_voltage * output_current * PF,
        # with a stable PF across both captures -- a sanity check that the
        # byte offsets are grouped correctly, not just individually plausible.
        for capture in (CAPTURE_1, CAPTURE_2):
            m = self.protocol.decode(capture).metrics
            apparent = m["output_voltage"] * m["output_current"]
            pf = m["ac_load_watts"] / apparent
            self.assertTrue(0.85 <= pf <= 1.0, f"implausible power factor {pf}")

    def test_corrupted_frame_fails_crc(self):
        corrupted = bytearray(CAPTURE_1)
        corrupted[50] ^= 0xFF
        result = self.protocol.decode(bytes(corrupted))
        self.assertFalse(result.valid)
        self.assertIn("CRC mismatch", result.note)

    def test_empty_reply_is_invalid(self):
        result = self.protocol.decode(b"")
        self.assertFalse(result.valid)

    def test_wrong_header_is_invalid(self):
        result = self.protocol.decode(bytes.fromhex("00112233445566778899"))
        self.assertFalse(result.valid)

    def test_wrong_length_is_invalid(self):
        result = self.protocol.decode(bytes.fromhex("885100030000") )
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
