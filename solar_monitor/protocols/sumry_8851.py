from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

from .base import DecodeResult, Query, Protocol

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
_READ_COMMAND = bytes.fromhex("0003")
_WRITE_COMMAND = bytes.fromhex("0010")
_CONFIG_ADDRESS = bytes.fromhex("0200")
_CONFIG_DATA_LENGTH = 94

# --- Writing settings ---------------------------------------------------------
# The inverter has no per-register write. A write is the whole 94-byte
# configuration block sent back with command 0x0010 in place of 0x0003 --
# the same layout it replies with, re-checksummed. That is the safety
# property this module enforces: a write frame is built only from a frame
# the inverter itself just sent, and may differ from it only in the fields
# listed below, within their ranges. Everything else goes back byte for byte.
#
# Ranges are the 24 V limits of the inverter's own menu (confirmed by the
# community 8851 integration); values it would refuse are refused here first.


@dataclass(frozen=True)
class WritableField:
    kind: str                      # "u16" scaled integer, or "bits" in a flag byte
    offset: int                    # frame byte offset (header included)
    scale: float = 1.0
    low: float = 0.0
    high: float = 0.0
    shift: int = 0                 # bit fields only
    mask: int = 0x01               # bit fields only, applied after shifting
    choices: tuple[str, ...] = ()  # bit fields with named values


# Fields whose byte mapping is confirmed against the inverter itself, either
# by the front panel or by observed behaviour. `battery_type` is deliberately
# absent: on 2026-09-05 the panel (menu 05) read AGM while this table's bits
# decoded "User-defined", proving bit 4 of byte 8 is something else. Writing
# it set an unidentified flag. Nothing goes back in here without a panel
# reading or a behavioural test to back it.
WRITABLE_FIELDS: dict[str, WritableField] = {
    # Byte 8 bits 3-4. The community struct labels these `battery_type`, and
    # this project believed that until 2026-09-05, when the inverter's own
    # panel (menu 05) read AGM while these bits decoded "User-defined". The
    # mapping is disproven for this firmware. They stay writable ONLY so the
    # value written on 2026-09-04 can be restored to 0; they must not be used
    # to select a battery type until the real register is identified.
    "unknown_flag_byte8_bits34": WritableField(
        "bits", 8, shift=3, mask=0x03, choices=("0", "1", "2", "3"),
    ),
    "bulk_charge_voltage_v": WritableField("u16", 48, 100, 24.0, 29.2),
    "float_charge_voltage_v": WritableField("u16", 52, 100, 24.0, 29.2),
    "low_battery_cutoff_v": WritableField("u16", 38, 100, 20.0, 24.0),
    "charge_cutoff_current_a": WritableField("u16", 60, 10, 2.0, 20.0),
    "maximum_charge_current_a": WritableField("u16", 58, 10, 10.0, 130.0),
    "overload_auto_restart": WritableField("bits", 11, shift=1, choices=("false", "true")),
    "overtemperature_auto_restart": WritableField("bits", 11, shift=6, choices=("false", "true")),
    "buzzer": WritableField("bits", 10, shift=1, choices=("false", "true")),
    "warning_buzzer": WritableField("bits", 10, shift=0, choices=("false", "true")),
    # No "equalization_enabled": the word at offset 82 was decoded as that
    # boolean until 2026-09-17. A full panel dump that day validated every
    # other setting in this block -- menu 34 29.2 V, 35 60 min, 36 120 min,
    # 37 30 days all matched offsets 90/92/94/96 exactly -- while menu 33 read
    # OFF and offset 82 held 4. It moved 0 -> 4 at 19:58:57 on 2026-09-16, the
    # moment battery type became User-defined, not when equalization changed.
    # So it is not an equalization on/off flag. It stays out of this table
    # because it was declared writable over 0..1 while holding 4: toggling it
    # would have written a boolean into a register that is something else.
    "equalization_voltage_v": WritableField("u16", 90, 100, 25.0, 29.5),
    "equalization_time_min": WritableField("u16", 92, 1, 5, 900),
    "equalization_timeout_min": WritableField("u16", 94, 1, 5, 900),
    "equalization_interval_days": WritableField("u16", 96, 1, 0, 90),
}



def _writable_masks() -> dict[int, int]:
    """Which bits of which bytes a write is allowed to change."""
    masks: dict[int, int] = {}
    for field in WRITABLE_FIELDS.values():
        if field.kind == "u16":
            masks[field.offset] = 0xFF
            masks[field.offset + 1] = 0xFF
        else:
            masks[field.offset] = masks.get(field.offset, 0) | (field.mask << field.shift)
    return masks


_WRITABLE_MASKS = _writable_masks()


_TRUTHY = {"true", "on", "1", "yes"}
_FALSY = {"false", "off", "0", "no"}


def _encode(field: WritableField, value: Any) -> int:
    if field.kind == "bits":
        if isinstance(value, bool):
            value = "true" if value else "false"
        if isinstance(value, str) and value in field.choices:
            return field.choices.index(value)
        raise ValueError(f"must be one of {', '.join(field.choices)}")
    if isinstance(value, bool):
        value = int(value)
    elif isinstance(value, str) and value.strip().lower() in _TRUTHY | _FALSY:
        value = int(value.strip().lower() in _TRUTHY)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("must be a number") from None
    if not field.low <= number <= field.high:
        raise ValueError(f"must be between {field.low:g} and {field.high:g}")
    return int(round(number * field.scale))


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


class Sumry8851Protocol(Protocol):
    """HGX 0x8851 protocol: allow-listed status and configuration reads with
    CRC-validated decoding, plus a configuration write built only from the
    inverter's own last reply and gated by `assert_write_frame`."""

    name = "sumry_8851"
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

    writable_fields = WRITABLE_FIELDS
    write_query_name = "write_settings_0200"

    def decode(self, frame: bytes) -> DecodeResult:
        if not frame:
            return DecodeResult({}, False, "No reply bytes to decode")
        if frame[:2] != _HEADER:
            return DecodeResult({}, False, "Reply missing expected 0x8851 header; raw bytes preserved")
        if frame[4:6] == bytes.fromhex("0200"):
            return self._decode_config(frame)
        return self._decode_state(frame)

    def build_config_write(self, reply: bytes, changes: dict[str, Any]) -> bytes:
        """The inverter's own configuration reply with `changes` applied and
        the command switched to write. Raises ValueError for a reply that is
        not a valid configuration frame, an unknown field, or a value the
        inverter's menu would not accept."""
        invalid = self._validate(reply, _CONFIG_FRAME_LENGTH, "configuration")
        if invalid:
            raise ValueError(f"Cannot write from this reply: {invalid.note}")
        if reply[2:4] != _READ_COMMAND or reply[4:6] != _CONFIG_ADDRESS:
            raise ValueError("Cannot write from this reply: not a configuration read")
        if not changes:
            raise ValueError("No settings to change")
        frame = bytearray(reply)
        frame[2:4] = _WRITE_COMMAND
        problems = []
        for name, value in changes.items():
            field = WRITABLE_FIELDS.get(name)
            if field is None:
                problems.append(f"{name}: not a writable setting")
                continue
            try:
                encoded = _encode(field, value)
            except ValueError as exc:
                problems.append(f"{name}: {exc}")
                continue
            if field.kind == "u16":
                struct.pack_into("<H", frame, field.offset, encoded)
            else:
                cleared = frame[field.offset] & ~(field.mask << field.shift)
                frame[field.offset] = cleared | ((encoded & field.mask) << field.shift)
        if problems:
            raise ValueError("; ".join(problems))
        struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
        built = bytes(frame)
        self.assert_write_frame(built, reply)
        return built

    def assert_write_frame(self, frame: bytes, template: bytes) -> None:
        """The gate every outbound write passes: a well-formed configuration
        write that differs from the inverter's own reply only in writable
        fields. Anything else is refused before it reaches the port."""
        if len(frame) != _CONFIG_FRAME_LENGTH or len(template) != _CONFIG_FRAME_LENGTH:
            raise PermissionError("Outbound write rejected: wrong frame length")
        if frame[:2] != _HEADER or frame[2:4] != _WRITE_COMMAND or frame[4:6] != _CONFIG_ADDRESS:
            raise PermissionError("Outbound write rejected: not a configuration write")
        if _u16(frame, 6) != _CONFIG_DATA_LENGTH:
            raise PermissionError("Outbound write rejected: wrong data length")
        if int.from_bytes(frame[-2:], "little") != _crc16_modbus(frame[:-2]):
            raise PermissionError("Outbound write rejected: checksum does not match")
        for index in range(8, _CONFIG_FRAME_LENGTH - 2):
            allowed = _WRITABLE_MASKS.get(index, 0)
            if (frame[index] ^ template[index]) & ~allowed & 0xFF:
                raise PermissionError(
                    f"Outbound write rejected: byte {index} is not a writable setting"
                )

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
            # NOT grid_current: offset 76 is deliberately absent. The community
            # struct labels it utility current, and this project published it
            # until 2026-09-16, when 4,489 grid-present frames disproved it. It
            # read exactly 0.00 through two nights while the utility carried
            # 200-400 W of load on its own (battery flat, PV zero), and rose to
            # 0.4-0.9 A at midday when the panels were carrying everything and
            # the utility was delivering nothing. Against the power balance it
            # correlates -0.48; against PV watts, +0.69. Whatever it counts, it
            # is not utility current, and no other u16 in the frame tracks
            # utility throughput. Utility use is derived instead -- see
            # solar_monitor/utility.py.
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
                "utility current is not decoded (offset 76 disproven 2026-09-16); "
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
        charge_priorities = ("PV & Grid", "PV then Grid", "PV only", "Unknown")
        settings = {
            "output_source_priority": ("PV > Grid > Battery", "PV > Battery > Grid")[(flag1 >> 2) & 1],
            "maximum_charge_current_a": _u16(frame, 58) / 10,
            "ac_input_range": ("UPS", "APL")[(flag0 >> 5) & 1],
            "power_saving": bool((flag2 >> 4) & 1),
            # NOT battery_type: see the note on WRITABLE_FIELDS. Reported raw
            # so a wrong label is never shown as fact.
            "unknown_flag_byte8_bits34": (flag0 >> 3) & 0x03,
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
            # NOT equalization_enabled: see the note in WRITABLE_FIELDS. Held
            # raw so a disproven label is never shown as fact, and so the
            # value can be compared against the panel if it changes again.
            "unknown_u16_offset82": _u16(frame, 82),
            "equalization_voltage_v": _u16(frame, 90) / 100,
            "equalization_time_min": _u16(frame, 92),
            "equalization_timeout_min": _u16(frame, 94),
            "equalization_interval_days": _u16(frame, 96),
            "inverter_power_limit_w": _u16(frame, 12),
        }
        return DecodeResult(
            settings, True,
            "CRC-validated HGX 0x8851 v2 current configuration; battery type is not "
            "decoded (byte 8 bits 3-4 disproven against panel menu 05 on 2026-09-05); "
            "equalization on/off is not decoded (offset 82 disproven against panel "
            "menu 33 on 2026-09-17)",
        )
