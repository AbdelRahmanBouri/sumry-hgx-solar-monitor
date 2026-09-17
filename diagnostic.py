#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from solar_monitor.protocols import create_protocol
from solar_monitor.serial_io import discover_usb_serial_ports, escaped_ascii, exchange


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only SUMRY HGX serial probe; outbound data is allow-listed."
    )
    parser.add_argument("--port", action="append", help="Port to test; repeatable (default: scan)")
    parser.add_argument("--baud", action="append", type=int, help="Baud to test; repeatable")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--listen-only", action="store_true", help="Open/read but transmit nothing")
    parser.add_argument("--json-log", type=Path, help="Append JSON Lines captures to this file")
    return parser.parse_args()


def listen(port: str, baudrate: int, timeout: float) -> bytes:
    import serial

    with serial.Serial(port, baudrate, timeout=timeout) as connection:
        return connection.read(4096)


def main() -> int:
    args = arguments()
    protocol = create_protocol("sumry_8851")
    ports = args.port or discover_usb_serial_ports()
    bauds = args.baud or [9600]
    if not ports:
        print("No serial devices found under /dev/ttyUSB* or /dev/serial/by-id/.", file=sys.stderr)
        return 2

    captures = []
    print("This probe transmits only the protocol's allow-listed read queries; it never writes to the inverter.")
    for port in ports:
        for baudrate in bauds:
            if args.listen_only:
                try:
                    response = listen(port, baudrate, args.timeout)
                    record = {"port": port, "baudrate": baudrate, "mode": "listen-only",
                              "response_hex": response.hex(" ").upper(),
                              "response_ascii": escaped_ascii(response), "error": None}
                except Exception as exc:
                    record = {"port": port, "baudrate": baudrate, "mode": "listen-only",
                              "response_hex": "", "response_ascii": "", "error": str(exc)}
                captures.append(record)
                print_record(record)
                continue
            for query in protocol.queries:
                result = exchange(port, baudrate, args.timeout, protocol, query)
                record = {
                    "timestamp": result.timestamp, "port": port, "baudrate": baudrate,
                    "mode": "query", "query": query.name,
                    "request_hex": result.request.hex(" ").upper(),
                    "response_hex": result.response.hex(" ").upper(),
                    "response_ascii": escaped_ascii(result.response),
                    "elapsed_ms": result.elapsed_ms, "error": result.error,
                }
                captures.append(record)
                print_record(record)
    if args.json_log:
        args.json_log.parent.mkdir(parents=True, exist_ok=True)
        with args.json_log.open("a", encoding="utf-8") as handle:
            for record in captures:
                handle.write(json.dumps(record) + "\n")
    return 0 if any(item.get("response_hex") for item in captures) else 1


def print_record(record: dict) -> None:
    print(f"\n{record['port']} @ {record['baudrate']}  [{record['mode']}]")
    if record.get("request_hex"):
        print(f"TX HEX: {record['request_hex']}")
    print(f"RX HEX: {record['response_hex'] or '(no data)'}")
    print(f"RX ASCII: {record['response_ascii'] or '(no data)'}")
    if record.get("error"):
        print(f"ERROR: {record['error']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())

