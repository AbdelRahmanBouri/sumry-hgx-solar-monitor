from __future__ import annotations

import logging
import threading

from .config import SerialConfig
from .db import Database
from .protocols import create_protocol
from .serial_io import discover_usb_serial_ports, escaped_ascii, exchange


LOG = logging.getLogger(__name__)


class Collector:
    def __init__(self, config: SerialConfig, database: Database):
        self.config = config
        self.database = database
        self.stop_event = threading.Event()
        self.protocol = create_protocol(config.protocol)

    def _port(self) -> str | None:
        if self.config.port != "auto":
            return self.config.port
        ports = discover_usb_serial_ports()
        return ports[0] if ports else None

    def collect_once(self) -> bool:
        port = self._port()
        if port is None:
            LOG.warning("No /dev/ttyUSB* or /dev/serial/by-id device found")
            return False
        query = self.protocol.queries[0]
        result = exchange(
            port, self.config.baudrate, self.config.timeout_seconds, self.protocol, query
        )
        decoded = self.protocol.decode(result.response)
        self.database.save_raw_frame({
            "captured_at": result.timestamp,
            "port": result.port,
            "baudrate": result.baudrate,
            "protocol": self.protocol.name,
            "query_name": result.query_name,
            "request_hex": result.request.hex(" ").upper(),
            "response_hex": result.response.hex(" ").upper(),
            "response_ascii": escaped_ascii(result.response),
            "elapsed_ms": result.elapsed_ms,
            "decode_note": decoded.note,
            "error": result.error,
        })
        if decoded.metrics:
            self.database.save_sample(result.timestamp, decoded.metrics)
        if result.error:
            LOG.warning("Serial exchange failed on %s: %s", port, result.error)
        else:
            LOG.info("Captured %d bytes from %s", len(result.response), port)
        return bool(result.response)

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.collect_once()
            self.stop_event.wait(max(30, self.config.poll_seconds))

    def stop(self) -> None:
        self.stop_event.set()


def start_collector(config: SerialConfig, database: Database) -> tuple[Collector, threading.Thread]:
    collector = Collector(config, database)
    thread = threading.Thread(target=collector.run_forever, name="serial-collector", daemon=True)
    thread.start()
    return collector, thread
