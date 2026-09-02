from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    pv_watts REAL, pv_voltage REAL, pv_current REAL,
    battery_voltage REAL, battery_current REAL,
    ac_load_watts REAL, ac_load_va REAL,
    output_voltage REAL, output_frequency REAL,
    inverter_temperature REAL, battery_temperature REAL,
    operating_state TEXT, faults TEXT
);
CREATE INDEX IF NOT EXISTS samples_captured_at ON samples(captured_at);
CREATE TABLE IF NOT EXISTS raw_frames (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    port TEXT NOT NULL,
    baudrate INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    query_name TEXT NOT NULL,
    request_hex TEXT NOT NULL,
    response_hex TEXT NOT NULL,
    response_ascii TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    decode_note TEXT,
    error TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def save_raw_frame(self, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO raw_frames ({columns}) VALUES ({placeholders})", values
            )

    def save_sample(self, captured_at: str, metrics: dict[str, Any]) -> None:
        allowed = {
            "pv_watts", "pv_voltage", "pv_current", "battery_voltage",
            "battery_current", "ac_load_watts", "ac_load_va", "output_voltage",
            "output_frequency", "inverter_temperature", "battery_temperature",
            "operating_state", "faults",
        }
        values = {key: value for key, value in metrics.items() if key in allowed}
        values["captured_at"] = captured_at
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        with self.connect() as connection:
            connection.execute(f"INSERT INTO samples ({columns}) VALUES ({placeholders})", values)

    def latest_sample(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM samples ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def history(self, since_iso: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM samples WHERE captured_at >= ? ORDER BY captured_at", (since_iso,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_diagnostic(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM raw_frames ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

