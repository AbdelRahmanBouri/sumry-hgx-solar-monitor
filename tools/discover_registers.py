#!/usr/bin/env python3
"""Map unknown configuration registers by changing one setting on the panel.

Roughly thirty of the 47 words in the HGX configuration block are still
unidentified, and two we *have* identified (bulk and float voltage) refuse
writes over the protocol. Either they are read-only here, or the real
setting lives at an offset we have not found -- and guessing by writing to
unknown registers is exactly the wrong way to find out.

This is the right way, and it never transmits anything. Mark a baseline,
change one setting on the inverter's own front panel, then diff: whichever
words moved are that setting's registers.

    python3 tools/discover_registers.py --mark
    # change ONE setting on the front panel, wait for a config poll (~5 min)
    python3 tools/discover_registers.py --diff

Reads the database only. It cannot write to the inverter.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solar_monitor.config import DEFAULT_PATH, load_config  # noqa: E402
from solar_monitor.protocols.sumry_8851 import WRITABLE_FIELDS  # noqa: E402

KNOWN = {
    8: "flags0/1", 10: "flags2/3", 12: "inverter_power_limit_w", 14: "output_voltage_v",
    16: "output_frequency_hz", 38: "low_battery_cutoff_v", 48: "bulk_charge_voltage_v",
    52: "float_charge_voltage_v", 54: "back_to_utility_voltage_v", 56: "utility_charge_current_a",
    58: "maximum_charge_current_a", 60: "charge_cutoff_current_a", 82: "unknown_u16_offset82",
    90: "equalization_voltage_v", 92: "equalization_time_min", 94: "equalization_timeout_min",
    96: "equalization_interval_days",
}
WRITABLE_OFFSETS = {field.offset for field in WRITABLE_FIELDS.values()}


def mark_file(database: str) -> Path:
    """The baseline lives beside the database it was taken from."""
    return Path(database).parent / "register-probe-mark"


def read_mark(mark: Path) -> tuple[str, bytes | None]:
    """(when the baseline was taken, the baseline frame).

    The frame is kept in the mark itself because routine frames age out of
    the database after a week; a baseline that relied on finding its own
    frame there would silently become whichever frame came next. A mark
    written before that holds only the timestamp, and gets the old
    behaviour: the first frame at or after it.
    """
    text = mark.read_text(encoding="utf-8").strip()
    try:
        stored = json.loads(text)
        return stored["captured_at"], bytes.fromhex(stored["response_hex"])
    except (ValueError, KeyError, TypeError):
        return text, None


def config_frames(database: str, since: str | None):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    query = ("SELECT captured_at, response_hex FROM raw_frames "
             "WHERE query_name='current_settings_0200' AND error IS NULL "
             "AND length(response_hex) > 200")
    params: list = []
    if since:
        query += " AND captured_at >= ?"
        params.append(since)
    rows = connection.execute(query + " ORDER BY id", params).fetchall()
    frames = [(r["captured_at"], bytes.fromhex(r["response_hex"].replace(" ", ""))) for r in rows]
    return [(when, frame) for when, frame in frames if len(frame) == 104]


def describe(offset: int, before: int, after: int) -> str:
    name = KNOWN.get(offset) or KNOWN.get(offset - 1)
    if name is None:
        label = "UNMAPPED"
    elif offset in WRITABLE_OFFSETS:
        label = f"{name} (already writable)"
    else:
        label = name
    scaled = ""
    if 1000 <= before <= 6000 and 1000 <= after <= 6000:
        scaled = f"   {before / 100:.2f} -> {after / 100:.2f} (as volts)"
    return f"  offset {offset:3d}  {before:6d} -> {after:6d}  {label}{scaled}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help=f"config file (default: {DEFAULT_PATH}, if present)")
    parser.add_argument("--mark", action="store_true", help="record a baseline and exit")
    parser.add_argument("--diff", action="store_true", help="report every word that moved since the baseline")
    args = parser.parse_args(argv)

    try:
        database = load_config(args.config or DEFAULT_PATH, required=args.config is not None).storage.database
    except FileNotFoundError as exc:
        parser.error(str(exc))
    mark = mark_file(database)
    if args.mark:
        frames = config_frames(database, None)
        if not frames:
            print("No configuration frames captured yet.", file=sys.stderr)
            return 1
        when, frame = frames[-1]
        mark.write_text(json.dumps({"captured_at": when, "response_hex": frame.hex()}), encoding="utf-8")
        print(f"Baseline marked at {when}.")
        print("Change ONE setting on the front panel, wait about five minutes for a")
        print("configuration poll, then run this again with --diff.")
        return 0

    if not args.diff:
        parser.error("pass --mark or --diff")
    if not mark.exists():
        print("No baseline. Run with --mark first.", file=sys.stderr)
        return 1

    since, baseline = read_mark(mark)
    frames = config_frames(database, since)
    if baseline is None and frames:
        baseline, frames = frames[0][1], frames[1:]
    else:
        frames = [(when, frame) for when, frame in frames if when > since]
    if not frames:
        print(f"No configuration poll since the baseline at {since[:19]}; wait for one.", file=sys.stderr)
        return 1

    moved: dict[int, tuple[int, int]] = {}
    for _when, frame in frames:
        for offset in range(8, len(frame) - 3, 2):
            before = struct.unpack_from("<H", baseline, offset)[0]
            after = struct.unpack_from("<H", frame, offset)[0]
            if before != after:
                moved[offset] = (before, after)

    span = f"{since[:19]} .. {frames[-1][0][:19]} ({len(frames)} frame{'' if len(frames) == 1 else 's'} since the baseline)"
    if not moved:
        print(f"Nothing moved between {span}.")
        print("Either the panel change has not been polled yet, or the inverter did not accept it.")
        return 0
    print(f"Words that moved between {span}:\n")
    for offset in sorted(moved):
        print(describe(offset, *moved[offset]))
    unmapped = [o for o in moved if not (KNOWN.get(o) or KNOWN.get(o - 1))]
    if unmapped:
        print(f"\n{len(unmapped)} unmapped word(s) moved: {', '.join(str(o) for o in unmapped)}")
        print("If one of those tracks the setting you changed, that is the register to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
