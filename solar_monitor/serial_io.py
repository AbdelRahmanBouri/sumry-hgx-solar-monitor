from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import glob
import os
import time
from typing import Any

import serial

from .protocols.base import Protocol, Query


@dataclass(frozen=True)
class Exchange:
    timestamp: str
    port: str
    baudrate: int
    query_name: str
    request: bytes
    response: bytes
    elapsed_ms: int
    error: str | None = None

    def record(self, protocol: str, note: str | None, decoded: bool) -> dict[str, Any]:
        """This exchange as a `raw_frames` row.

        `decoded` says whether the reply decoded into readings. Retention
        keys on it: a decoded frame is routine and ages out quickly, anything
        else is kept as a diagnostic.
        """
        return {
            "captured_at": self.timestamp,
            "port": self.port,
            "baudrate": self.baudrate,
            "protocol": protocol,
            "query_name": self.query_name,
            "request_hex": self.request.hex(" ").upper(),
            "response_hex": self.response.hex(" ").upper(),
            "response_ascii": escaped_ascii(self.response),
            "elapsed_ms": self.elapsed_ms,
            "decode_note": note,
            "error": self.error,
            "decoded": int(decoded),
        }


def discover_usb_serial_ports() -> list[str]:
    paths = glob.glob("/dev/serial/by-id/*") + glob.glob("/dev/ttyUSB*")
    unique: dict[str, str] = {}
    for path in paths:
        unique.setdefault(os.path.realpath(path), path)
    return sorted(unique.values())


def escaped_ascii(data: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in data)


def exchange(
    port: str,
    baudrate: int,
    timeout: float,
    protocol: Protocol,
    query: Query,
    settle_seconds: float = 0.15,
) -> Exchange:
    protocol.assert_read_only_frame(query.frame)
    return _transact(port, baudrate, timeout, query.name, query.frame, settle_seconds)


def write_exchange(
    port: str,
    baudrate: int,
    timeout: float,
    protocol,
    frame: bytes,
    template: bytes,
    settle_seconds: float = 0.15,
) -> Exchange:
    """Send a configuration write. The protocol's write gate runs first, so
    a frame that is not a permitted change of the inverter's own last reply
    never reaches the port."""
    protocol.assert_write_frame(frame, template)
    return _transact(port, baudrate, timeout, protocol.write_query_name, frame, settle_seconds)


def _transact(
    port: str,
    baudrate: int,
    timeout: float,
    name: str,
    frame: bytes,
    settle_seconds: float,
) -> Exchange:
    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        ) as connection:
            connection.reset_input_buffer()
            time.sleep(settle_seconds)
            connection.write(frame)
            connection.flush()
            response = connection.read(512)
            # Collect bytes that arrive in short trailing bursts.
            trailing_deadline = time.monotonic() + 0.12
            while time.monotonic() < trailing_deadline:
                waiting = connection.in_waiting
                if waiting:
                    response += connection.read(min(waiting, 4096 - len(response)))
                    trailing_deadline = time.monotonic() + 0.05
                else:
                    time.sleep(0.01)
            return Exchange(
                timestamp, port, baudrate, name, frame, response,
                round((time.monotonic() - started) * 1000),
            )
    except (serial.SerialException, OSError) as exc:
        return Exchange(
            timestamp, port, baudrate, name, frame, b"",
            round((time.monotonic() - started) * 1000), str(exc),
        )
