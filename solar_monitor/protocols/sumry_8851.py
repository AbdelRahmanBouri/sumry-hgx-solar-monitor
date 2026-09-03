from __future__ import annotations

import struct

from .base import DecodeResult, Query, ReadOnlyProtocol

# --- Frame layout -----------------------------------------------------------
# Both real HGX status replies captured 2026-09-02 pass their trailing
# CRC16/MODBUS checksum, confirming that they are intact 158-byte frames.
# The field offsets below follow the independently reverse-engineered 0x8851
# v2 register layout and are supported by internally consistent values:
# load_watts / (output_
# voltage * output_current) gives ~0.92 power factor on both captures,
# battery_voltage sits at ~27V (right for a 24V nominal bank), and output
# is 120V/59.9Hz, matching this inverter's 24V/120V spec.
_HEADER = bytes.fromhex("8851")
_FRAME_LENGTH = 158
_CONFIG_FRAME_LENGTH = 104


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


class Sumry8851ProbeProtocol(ReadOnlyProtocol):
    """Read-only HGX status query with validated frame and field decoding."""

    name = "sumry_8851_probe"
    queries = (
        Query(
            name="status_candidate_0003",
            frame=bytes.fromhex("88 51 00 03 00 00 00 00 4D 08"),
            description="Candidate binary 0x8851 status query supplied for HGX firmware",
        ),
        Query(
            name="current_settings_0200",
            frame=bytes.fromhex("88 51 00 03 02 00 00 00 4C B0"),
            description="Read-only 0x8851 configuration query for address 0x0200",
        ),
    )

    def decode(self, frame: bytes) -> DecodeResult:
        if not frame:
            return DecodeResult({}, False, "No reply bytes to decode")
        if frame[:2] != _HEADER:
            return DecodeResult({}, False, "Reply missing expected 0x8851 header; raw bytes preserved")
        if frame[4:6] == bytes.fromhex("0200"):
            return self._decode_config(frame)
        return self._decode_state(frame)

    def _validate(self, frame: bytes, expected_length: int, kind: str) -> DecodeResult | None:
        if len(frame) != expected_length:
            return DecodeResult(
                {}, False,
                f"Expected a {expected_length}-byte HGX {kind} frame, got {len(frame)}; raw bytes preserved",
            )
        expected_crc = int.from_bytes(frame[-2:], "little")
        calculated_crc = _crc16_modbus(frame[:-2])
        if expected_crc != calculated_crc:
            return DecodeResult(
                {}, False,
                f"CRC mismatch (expected {expected_crc:04X}, calculated {calculated_crc:04X}); "
                "raw bytes preserved, frame likely corrupted in transit",
            )
        return None

    def _decode_state(self, frame: bytes) -> DecodeResult:
        invalid = self._validate(frame, _FRAME_LENGTH, "status")
        if invalid:
            return invalid

        metrics = {
            "output_voltage": _u16(frame, 50) / 10,
            "output_current": _u16(frame, 52) / 100,
            "output_frequency": _u16(frame, 54) / 100,
            "inverter_va": _u16(frame, 56),
            "ac_load_va": _u16(frame, 58),
            "ac_load_watts": _u16(frame, 62),
            "ac_load_current": _u16(frame, 68) / 100,
            "grid_voltage": _u16(frame, 74) / 10,
            "grid_current": _u16(frame, 76) / 100,
            "grid_frequency": _u16(frame, 78) / 100,
            "battery_voltage": _u16(frame, 86) / 100,
            "battery_current": _s16(frame, 88) / 10,
            "pv_voltage": _u16(frame, 94) / 10,
            "pv_current": _u16(frame, 96) / 100,
            "pv_watts": _u16(frame, 98),
            "bus_voltage": _u16(frame, 100) / 10,
            "fan1_percent": frame[108],
            "fan2_percent": frame[109],
            # These are inverter thermistors in the published community map.
            # The following byte (113) is the separate BTS input. It is 25 in
            # our captures, but this installation has no confirmed battery
            # temperature probe, so it must not be presented as battery temp.
            "ntc2_temperature": frame[110],
            "ntc3_temperature": frame[111],
            "ntc4_temperature": frame[112],
            "bts_temperature": frame[113],
        }
        return DecodeResult(
            metrics=metrics, valid=True,
            note=(
                "CRC-validated HGX 0x8851 v2 status frame; BTS is stored as an input value "
                "but is not a confirmed battery temperature without a connected probe; "
                "operating_state/faults byte(s) not yet identified"
            ),
        )

    def _decode_config(self, frame: bytes) -> DecodeResult:
        invalid = self._validate(frame, _CONFIG_FRAME_LENGTH, "configuration")
        if invalid:
            return invalid
        if frame[2:4] != bytes.fromhex("0003") or _u16(frame, 6) != 94:
            return DecodeResult({}, False, "Unexpected HGX configuration response header")

        flag0, flag1, flag2, flag3 = frame[8:12]
        battery_types = ("AGM", "Flooded", "User-defined", "Lithium/BMS library")
        charge_priorities = ("PV & Grid", "PV then Grid", "PV only", "Unknown")
        settings = {
            "output_source_priority": ("PV > Grid > Battery", "PV > Battery > Grid")[(flag1 >> 2) & 1],
            "maximum_charge_current_a": _u16(frame, 58) / 10,
            "ac_input_range": ("UPS", "APL")[(flag0 >> 5) & 1],
            "power_saving": bool((flag2 >> 4) & 1),
            "battery_type": battery_types[(flag0 >> 3) & 0x03],
            "overload_auto_restart": bool((flag3 >> 1) & 1),
            "overtemperature_auto_restart": bool((flag3 >> 6) & 1),
            "output_voltage_v": _u16(frame, 14) / 10,
            "output_frequency_mode": ("50 Hz", "60 Hz")[(flag0 >> 7) & 1],
            "output_frequency_hz": _u16(frame, 16) / 100,
            "grid_export_enabled": bool((flag1 >> 6) & 1),
            "utility_charge_current_a": _u16(frame, 56) / 10,
            "charge_cutoff_current_a": _u16(frame, 60) / 10,
            "back_to_utility_voltage_v": _u16(frame, 54) / 100,
            "parallel_operation": bool(flag0 & 1),
            "phase": ("A", "B", "C", "Unknown")[(flag0 >> 1) & 0x03],
            "charge_source_priority": charge_priorities[(flag1 >> 4) & 0x03],
            "buzzer": bool((flag2 >> 1) & 1),
            "warning_buzzer": bool(flag2 & 1),
            "auto_return_display": bool((flag2 >> 6) & 1),
            "backlight": bool((flag2 >> 7) & 1),
            "power_interruption_buzzer": bool((flag1 >> 7) & 1),
            "fault_recording": bool((flag3 >> 7) & 1),
            "low_battery_cutoff_v": _u16(frame, 38) / 100,
            "bulk_charge_voltage_v": _u16(frame, 48) / 100,
            "float_charge_voltage_v": _u16(frame, 52) / 100,
            "equalization_enabled": bool(_u16(frame, 82)),
            "equalization_voltage_v": _u16(frame, 90) / 100,
            "equalization_time_min": _u16(frame, 92),
            "equalization_timeout_min": _u16(frame, 94),
            "equalization_interval_days": _u16(frame, 96),
            "inverter_power_limit_w": _u16(frame, 12),
        }
        return DecodeResult(settings, True, "CRC-validated HGX 0x8851 v2 current configuration")
