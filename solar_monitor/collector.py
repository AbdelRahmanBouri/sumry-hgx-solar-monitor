from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time

from . import change_policy
from .config import SerialConfig
from .db import Database
from .protocols import create_protocol
from .serial_io import discover_usb_serial_ports, escaped_ascii, exchange, write_exchange


LOG = logging.getLogger(__name__)

# systemd-timesyncd creates this once the clock has been set from the network.
_SYNC_FLAG = Path("/run/systemd/timesync/synchronized")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_setting(requested, reported) -> bool:
    """A requested value matches a decoded read-back: numbers to the
    resolution the inverter stores, switches by truth, names exactly."""
    def truth(value):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "on", "yes", "1", "1.0"}:
            return True
        if text in {"false", "off", "no", "0", "0.0"}:
            return False
        return None

    if isinstance(requested, bool) or isinstance(reported, bool):
        return truth(requested) is not None and truth(requested) == truth(reported)
    try:
        return abs(float(requested) - float(reported)) < 0.051
    except (TypeError, ValueError):
        return str(requested) == str(reported)


class Collector:
    def __init__(self, config: SerialConfig, database: Database):
        self.config = config
        self.database = database
        self.stop_event = threading.Event()
        self.settings_event = threading.Event()
        self._interval_seconds = max(10, config.poll_seconds)
        self.protocol = create_protocol(config.protocol)
        # Guards collect_once() so a manually triggered "pull now" refresh
        # from the web UI can never overlap the background poll on the same
        # serial port.
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_at: str | None = None
        self.last_poll_at: str | None = None
        self.last_success_at: str | None = None
        self.last_error: str | None = None
        self.last_port: str | None = None
        self.polls = 0
        self.failures = 0
        # A failed exchange is a failure. Counting only unhandled exceptions
        # meant 425 consecutive dead polls reported failures=0 on 2026-09-05,
        # so nothing could ever alarm on a serial adapter that had stopped
        # answering. The streak is what a watchdog actually needs.
        self.consecutive_failures = 0
        self.failing_since: str | None = None
        self.last_maintenance_at: str | None = None
        self.last_maintenance_result: dict | None = None
        self._maintenance_monotonic = 0.0
        self._newest_stored: str | None = None

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    def set_poll_seconds(self, seconds: int) -> None:
        if not 10 <= seconds <= 3600:
            raise ValueError("Polling must be between 10 and 3600 seconds")
        self._interval_seconds = seconds
        self.settings_event.set()

    def state(self) -> dict:
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "started_at": self.started_at,
            "last_poll_at": self.last_poll_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_port": self.last_port,
            "polls": self.polls,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "failing_since": self.failing_since,
            "last_maintenance_at": self.last_maintenance_at,
            "last_maintenance_result": self.last_maintenance_result,
            "interval_seconds": self.interval_seconds,
            "protocol": self.protocol.name,
            "configured_port": self.config.port,
        }

    def _port(self) -> str | None:
        if self.config.port != "auto":
            return self.config.port
        ports = discover_usb_serial_ports()
        return ports[0] if ports else None

    def collect_once(self) -> bool:
        with self.lock:
            self.polls += 1
            self.last_poll_at = _now()
            port = self._port()
            if port is None:
                self.last_error = "No /dev/ttyUSB* or /dev/serial/by-id device found"
                LOG.warning("%s", self.last_error)
                return False
            if port != self.last_port:
                LOG.info("Collector polling %s at %d baud", port, self.config.baudrate)
                self.last_port = port
            # Status is sampled every cycle. Configuration is read at startup
            # and every ten cycles (~5 minutes at the default interval).
            query = self.protocol.queries[0]
            success = self._collect_query(port, query)
            if self.polls == 1 or self.polls % 10 == 0:
                self._collect_query(port, self.protocol.query("current_settings_0200"))
            self._run_maintenance_if_due()
            return success

    def apply_config(self, changes: dict, settle_seconds: float = 0.6) -> dict:
        """Change inverter settings: read the current block, write it back
        with `changes`, then read again and report what the inverter now
        says. Holds the port lock throughout so no poll can interleave.

        The result is never "done" on the strength of having sent the frame.
        `verified` is true only when the read-back carries every requested
        value; `mismatches` lists the ones it does not.
        """
        if not hasattr(self.protocol, "build_config_write"):
            raise ValueError(f"Protocol {self.protocol.name} does not support writes")
        with self.lock:
            port = self._port()
            if port is None:
                raise ValueError("No /dev/ttyUSB* or /dev/serial/by-id device found")
            read_query = self.protocol.query("current_settings_0200")

            before = exchange(port, self.config.baudrate, self.config.timeout_seconds, self.protocol, read_query)
            decoded_before = self.protocol.decode(before.response)
            self._record(before, decoded_before)
            if before.error or not decoded_before.valid:
                raise ValueError(
                    f"Could not read the current configuration: {before.error or decoded_before.note}"
                )

            frame = self.protocol.build_config_write(before.response, changes)
            written = write_exchange(
                port, self.config.baudrate, self.config.timeout_seconds, self.protocol, frame, before.response
            )
            self.database.save_raw_frame({
                "captured_at": written.timestamp, "port": written.port, "baudrate": written.baudrate,
                "protocol": self.protocol.name, "query_name": written.query_name,
                "request_hex": written.request.hex(" ").upper(),
                "response_hex": written.response.hex(" ").upper(),
                "response_ascii": escaped_ascii(written.response),
                "elapsed_ms": written.elapsed_ms,
                "decode_note": f"configuration write: {sorted(changes)}",
                "error": written.error,
            })
            if written.error:
                raise ValueError(f"Write failed on the serial port: {written.error}")

            time.sleep(settle_seconds)
            after = exchange(port, self.config.baudrate, self.config.timeout_seconds, self.protocol, read_query)
            decoded_after = self.protocol.decode(after.response)
            self._record(after, decoded_after)
            if after.error or not decoded_after.valid:
                raise ValueError(
                    "The write was sent but the configuration could not be read back: "
                    f"{after.error or decoded_after.note}"
                )

            mismatches = {}
            for name, wanted in changes.items():
                got = decoded_after.metrics.get(name)
                if not _same_setting(wanted, got):
                    mismatches[name] = {"requested": wanted, "inverter": got}
            return {
                "requested": changes,
                "before": decoded_before.metrics,
                "after": decoded_after.metrics,
                "verified": not mismatches,
                "mismatches": mismatches,
                "written_at": written.timestamp,
                "write_reply_hex": written.response.hex(" ").upper(),
            }

    def apply_approved_changes(self) -> list[dict]:
        """Put approved change requests on the wire, one at a time, between
        polls. This runs in the process that owns the serial port -- the whole
        reason proposals are queued rather than applied by whoever made them.

        Each request is claimed before it is sent, so a request cannot be
        applied twice even if this is called concurrently.
        """
        if not hasattr(self.database, "change_requests"):
            return []
        outcomes = []
        for request in reversed(self.database.change_requests(20, statuses=("approved",))):
            if not self.database.claim_change_request(request["id"]):
                continue  # someone else took it
            try:
                result = self.apply_config(request["changes"])
                status = "applied" if result["verified"] else "unverified"
            except (ValueError, PermissionError) as exc:
                result = {"error": str(exc)}
                status = "failed"
                LOG.warning("Change request %d failed: %s", request["id"], exc)
            self.database.finish_change_request(request["id"], status, _now(), result)
            if hasattr(self.database, "audit"):
                self.database.audit(
                    _now(), request.get("decided_by"), f"inverter.change.{status}",
                    f"request {request['id']}: {request['changes']}",
                )
            outcomes.append({"id": request["id"], "status": status, "result": result})
        return outcomes

    def _timestamp_is_trustworthy(self, timestamp: str) -> bool:
        """A Raspberry Pi has no RTC: on a cold boot it restores a stale clock
        until NTP syncs. On 2026-09-05 that put two real readings nearly ten
        hours in the past, showing 599 W of sun at midnight. Two cheap guards:
        the system's own sync flag, and never accepting a reading stamped
        before one already stored."""
        if _SYNC_FLAG.parent.exists() and not _SYNC_FLAG.exists():
            return False
        if self._newest_stored is None:
            latest = getattr(self.database, "latest_sample", lambda: None)()
            self._newest_stored = (latest or {}).get("captured_at") or ""
        return timestamp >= self._newest_stored

    def _record(self, result, decoded) -> None:
        self.database.save_raw_frame({
            "captured_at": result.timestamp, "port": result.port, "baudrate": result.baudrate,
            "protocol": self.protocol.name, "query_name": result.query_name,
            "request_hex": result.request.hex(" ").upper(),
            "response_hex": result.response.hex(" ").upper(),
            "response_ascii": escaped_ascii(result.response),
            "elapsed_ms": result.elapsed_ms, "decode_note": decoded.note, "error": result.error,
        })
        if decoded.metrics and result.query_name == "current_settings_0200":
            self.database.save_inverter_config(result.timestamp, decoded.metrics)

    def _run_maintenance_if_due(self, force: bool = False) -> dict | None:
        if not hasattr(self.database, "cleanup_retention"):
            return None
        current = time.monotonic()
        if not force and self._maintenance_monotonic and current - self._maintenance_monotonic < 86400:
            return None
        settings = self.database.get_settings() if hasattr(self.database, "get_settings") else {}
        result = self.database.cleanup_retention(
            int(settings.get("raw_success_days", "7")),
            int(settings.get("raw_diagnostic_days", "90")),
        )
        self._maintenance_monotonic = current
        self.last_maintenance_at = _now()
        self.last_maintenance_result = result
        LOG.info("Storage maintenance complete: %s", result)
        return result

    def _collect_query(self, port, query) -> bool:
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
        if decoded.metrics and not self._timestamp_is_trustworthy(result.timestamp):
            LOG.warning(
                "Discarding a reading stamped %s: the clock is not yet trustworthy",
                result.timestamp,
            )
        elif decoded.metrics and query.name == "current_settings_0200":
            self.database.save_inverter_config(result.timestamp, decoded.metrics)
            self._newest_stored = max(self._newest_stored or "", result.timestamp)
        elif decoded.metrics:
            self.database.save_sample(result.timestamp, decoded.metrics)
            self._newest_stored = max(self._newest_stored or "", result.timestamp)
        if result.error:
            self.last_error = result.error
            self.failures += 1
            self.consecutive_failures += 1
            self.failing_since = self.failing_since or result.timestamp
            LOG.warning(
                "Serial exchange failed on %s (%d in a row since %s): %s",
                port, self.consecutive_failures, self.failing_since, result.error,
            )
        else:
            self.last_error = None
            self.consecutive_failures = 0
            self.failing_since = None
            self.last_success_at = result.timestamp
            LOG.info(
                "Captured %d bytes from %s (%s)",
                len(result.response), port,
                "decoded" if decoded.metrics else f"not decoded: {decoded.note}",
            )
        return bool(result.response)

    def run_forever(self) -> None:
        self.started_at = _now()
        LOG.info(
            "Collector starting: protocol=%s port=%s baud=%d interval=%ds",
            self.protocol.name, self.config.port, self.config.baudrate, self.interval_seconds,
        )
        while not self.stop_event.is_set():
            try:
                self.collect_once()
                # Outside collect_once's lock: apply_config takes the same
                # non-reentrant lock, so nesting them would deadlock.
                self.apply_approved_changes()
            except Exception as exc:  # never let one bad cycle kill the poller
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.exception("Collector cycle failed; continuing")
            self.settings_event.wait(self.interval_seconds)
            self.settings_event.clear()
        LOG.info("Collector stopped after %d polls (%d failures)", self.polls, self.failures)

    def stop(self) -> None:
        self.stop_event.set()
        self.settings_event.set()


def start_collector(config: SerialConfig, database: Database) -> tuple[Collector, threading.Thread]:
    collector = Collector(config, database)
    thread = threading.Thread(target=collector.run_forever, name="serial-collector", daemon=True)
    collector.thread = thread
    thread.start()
    return collector, thread
