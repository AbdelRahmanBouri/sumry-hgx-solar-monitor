"""CRC-valid but structurally invalid frames must still be refused."""
import unittest
from unittest.mock import patch

import serial

from solar_monitor.protocols.base import Protocol
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol, _crc16_modbus
from solar_monitor.serial_io import discover_usb_serial_ports, exchange, write_exchange
from tests.test_decode import CAPTURE_1
from tests.test_safety import REAL_CONFIG_REPLY


def with_bytes(frame, start, replacement):
    changed = bytearray(frame)
    changed[start:start + len(replacement)] = replacement
    changed[-2:] = _crc16_modbus(changed[:-2]).to_bytes(2, 'little')
    return bytes(changed)


class ProtocolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.protocol = Sumry8851Protocol()

    def test_configuration_must_have_valid_length_crc_command_and_declared_size(self):
        invalid = [REAL_CONFIG_REPLY[:-1], REAL_CONFIG_REPLY[:-2] + b'\x00\x00',
                   with_bytes(REAL_CONFIG_REPLY, 2, b'\x00\x10'),
                   with_bytes(REAL_CONFIG_REPLY, 6, b'\x60\x00')]
        for frame in invalid:
            with self.subTest(frame=frame[:8]):
                result = self.protocol.decode(frame)
                self.assertFalse(result.valid)
                self.assertEqual(result.metrics, {})
                with self.assertRaises((ValueError, PermissionError)):
                    self.protocol.build_config_write(frame, {'buzzer': False})

    def test_status_with_valid_crc_still_requires_the_exact_address_and_declared_size(self):
        for offset, value in ((4, b'\x01\x00'), (6, b'\x96\x00')):
            result = self.protocol.decode(with_bytes(CAPTURE_1, offset, value))
            self.assertFalse(result.valid)
            self.assertEqual(result.metrics, {})

    def test_each_single_bit_error_in_a_capture_is_detected(self):
        for capture in (CAPTURE_1, REAL_CONFIG_REPLY):
            for index in range(len(capture)):
                for bit in range(8):
                    changed = bytearray(capture)
                    changed[index] ^= 1 << bit
                    with self.subTest(length=len(capture), byte=index, bit=bit):
                        self.assertFalse(self.protocol.decode(bytes(changed)).valid)

    def test_signed_discharge_current_is_not_decoded_as_a_large_positive_current(self):
        result = self.protocol.decode(with_bytes(CAPTURE_1, 88, (-123).to_bytes(2, 'little', signed=True)))
        self.assertTrue(result.valid)
        self.assertEqual(result.metrics['battery_current'], -12.3)

    def test_invalid_write_envelopes_never_open_a_port(self):
        good = self.protocol.build_config_write(REAL_CONFIG_REPLY, {'buzzer': False})
        bad_templates = [REAL_CONFIG_REPLY[:-1], with_bytes(REAL_CONFIG_REPLY, 0, b'\x00\x00'),
                         with_bytes(REAL_CONFIG_REPLY, 4, b'\x00\x00'),
                         with_bytes(REAL_CONFIG_REPLY, 2, b'\x00\x10')]
        pairs = [(good, template) for template in bad_templates]
        pairs += [(good[:-1], REAL_CONFIG_REPLY),
                  (with_bytes(good, 6, b'\x60\x00'), REAL_CONFIG_REPLY),
                  (with_bytes(good, 8, bytes([good[8] | 0x08])), REAL_CONFIG_REPLY)]
        with patch('solar_monitor.serial_io.serial.serial_for_url') as open_port:
            for frame, template in pairs:
                with self.subTest(frame=frame[:10], template=template[:10]), self.assertRaises(PermissionError):
                    write_exchange('sim://', 9600, 1, self.protocol, frame, template)
        open_port.assert_not_called()

    def test_numeric_restoration_preserves_every_other_bit(self):
        template = with_bytes(REAL_CONFIG_REPLY, 8, bytes([REAL_CONFIG_REPLY[8] | 0x18]))
        frame = self.protocol.build_config_write(template, {'unknown_flag_byte8_bits34': 0})
        self.assertEqual(frame[8], template[8] & ~0x18)
        self.assertEqual(frame[9:-2], template[9:-2])

    def test_serial_open_errors_are_returned_with_the_attempted_query(self):
        query = self.protocol.queries[0]
        for error in (serial.SerialException('disconnected'), OSError('permission denied'), ValueError('bad port')):
            with self.subTest(error=error), patch('solar_monitor.serial_io.serial.serial_for_url', side_effect=error):
                result = exchange('sim://', 9600, 1, self.protocol, query)
                self.assertEqual(result.error, str(error))
                self.assertEqual(result.request, query.frame)
                self.assertEqual(result.response, b'')

    def test_discovery_keeps_stable_device_names_without_duplicate_handles(self):
        paths = {'/dev/serial/by-id/adapter': '/dev/ttyUSB0', '/dev/ttyUSB0': '/dev/ttyUSB0',
                 '/dev/ttyUSB1': '/dev/ttyUSB1'}
        with patch('solar_monitor.serial_io.glob.glob', side_effect=[['/dev/serial/by-id/adapter'],
                                                                    ['/dev/ttyUSB0', '/dev/ttyUSB1']]), \
             patch('solar_monitor.serial_io.os.path.realpath', side_effect=paths.__getitem__):
            self.assertEqual(discover_usb_serial_ports(), ['/dev/serial/by-id/adapter', '/dev/ttyUSB1'])

    def test_unconfirmed_protocol_exposes_no_decode_or_query_claim(self):
        protocol = Protocol()
        self.assertFalse(protocol.decode(CAPTURE_1).valid)
        with self.assertRaises(ValueError):
            protocol.query('made_up_query')
