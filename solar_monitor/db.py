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
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    detail TEXT
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

    def admin_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])

    def create_admin(self, username: str, password_hash: str, created_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, created_at),
            )

    def admin_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def record_login(self, username: str, timestamp: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE admin_users SET last_login_at = ? WHERE username = ?",
                (timestamp, username),
            )

    def get_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_settings(self, values: dict[str, str], timestamp: str) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(key, value, timestamp) for key, value in values.items()],
            )

    def audit(self, occurred_at: str, username: str | None, action: str, detail: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events (occurred_at, username, action, detail) VALUES (?, ?, ?, ?)",
                (occurred_at, username, action, detail),
            )

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def raw_frames(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM raw_frames ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
