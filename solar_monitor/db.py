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
    ac_load_watts REAL, ac_load_va REAL, ac_load_current REAL,
    output_voltage REAL, output_frequency REAL, output_current REAL,
    grid_voltage REAL, grid_frequency REAL, grid_current REAL,
    inverter_va REAL, bus_voltage REAL,
    fan1_percent REAL, fan2_percent REAL,
    temperature_1 REAL, temperature_2 REAL, temperature_3 REAL,
    inverter_temperature REAL, battery_temperature REAL,
    ntc2_temperature REAL, ntc3_temperature REAL, ntc4_temperature REAL,
    bts_temperature REAL,
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
CREATE INDEX IF NOT EXISTS raw_frames_captured_at ON raw_frames(captured_at);
CREATE TABLE IF NOT EXISTS inverter_config_snapshots (
    id INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inverter_config_captured_at ON inverter_config_snapshots(captured_at);
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    pin_hash TEXT,
    pin_failures INTEGER NOT NULL DEFAULT 0,
    pin_locked INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS inverter_change_requests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT,
    applied_at TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS change_requests_status ON inverter_change_requests(status);
"""

# Metric columns accepted from a decoder's DecodeResult.metrics. Kept as an
# explicit allow-list (rather than trusting whatever a protocol module
# returns) so a future decoder can't accidentally write to arbitrary columns.
SAMPLE_METRIC_COLUMNS = {
    "pv_watts", "pv_voltage", "pv_current",
    "battery_voltage", "battery_current",
    "ac_load_watts", "ac_load_va", "ac_load_current",
    "output_voltage", "output_frequency", "output_current",
    "grid_voltage", "grid_frequency",
    # No "grid_current": offset 76 carried that name until 2026-09-16 and was
    # disproven against two weeks of readings. The column stays in the schema
    # for the rows already written, but nothing may fill it again.
    "inverter_va", "bus_voltage",
    "fan1_percent", "fan2_percent",
    "temperature_1", "temperature_2", "temperature_3",
    "inverter_temperature", "battery_temperature",
    "ntc2_temperature", "ntc3_temperature", "ntc4_temperature",
    "bts_temperature",
    "operating_state", "faults",
}


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        admin_columns = {row[1] for row in connection.execute("PRAGMA table_info(admin_users)")}
        admin_additions = {
            "pin_hash": "TEXT",
            "pin_failures": "INTEGER NOT NULL DEFAULT 0",
            "pin_locked": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in admin_additions.items():
            if name not in admin_columns:
                connection.execute(f"ALTER TABLE admin_users ADD COLUMN {name} {declaration}")

        sample_columns = {row[1] for row in connection.execute("PRAGMA table_info(samples)")}
        sample_additions = {
            "ac_load_current": "REAL",
            "output_current": "REAL",
            "grid_voltage": "REAL",
            "grid_frequency": "REAL",
            "grid_current": "REAL",
            "inverter_va": "REAL",
            "bus_voltage": "REAL",
            "fan1_percent": "REAL",
            "fan2_percent": "REAL",
            "temperature_1": "REAL",
            "temperature_2": "REAL",
            "temperature_3": "REAL",
            "ntc2_temperature": "REAL",
            "ntc3_temperature": "REAL",
            "ntc4_temperature": "REAL",
            "bts_temperature": "REAL",
        }
        for name, declaration in sample_additions.items():
            if name not in sample_columns:
                connection.execute(f"ALTER TABLE samples ADD COLUMN {name} {declaration}")

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
        values = {key: value for key, value in metrics.items() if key in SAMPLE_METRIC_COLUMNS}
        values["captured_at"] = captured_at
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        with self.connect() as connection:
            connection.execute(f"INSERT INTO samples ({columns}) VALUES ({placeholders})", values)

    def save_inverter_config(self, captured_at: str, settings: dict[str, Any]) -> None:
        import json
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO inverter_config_snapshots (captured_at, config_json) VALUES (?, ?)",
                (captured_at, json.dumps(settings, sort_keys=True)),
            )

    def latest_inverter_config(self) -> dict[str, Any] | None:
        import json
        with self.connect() as connection:
            row = connection.execute(
                "SELECT captured_at, config_json FROM inverter_config_snapshots "
                "ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"captured_at": row["captured_at"], "settings": json.loads(row["config_json"])}

    def inverter_config_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        import json
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, captured_at, config_json FROM inverter_config_snapshots "
                "ORDER BY captured_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": row["id"], "captured_at": row["captured_at"], "settings": json.loads(row["config_json"])}
            for row in rows
        ]

    def inverter_config_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        import json
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, captured_at, config_json FROM inverter_config_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "captured_at": row["captured_at"], "settings": json.loads(row["config_json"])}

    # --- Inverter change requests -------------------------------------------
    # A proposal is not a write. It records an intent that something else --
    # an admin clicking approve, or a standing policy -- has to release before
    # the collector will put it on the wire.

    def create_change_request(
        self, created_at: str, requested_by: str, reason: str, changes: dict[str, Any],
        status: str = "pending", decided_by: str | None = None, decided_at: str | None = None,
    ) -> int:
        import json
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO inverter_change_requests "
                "(created_at, requested_by, reason, changes_json, status, decided_by, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, requested_by, reason, json.dumps(changes, sort_keys=True),
                 status, decided_by, decided_at),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _change_request(row) -> dict[str, Any]:
        import json
        record = dict(row)
        record["changes"] = json.loads(record.pop("changes_json"))
        result = record.pop("result_json", None)
        record["result"] = json.loads(result) if result else None
        return record

    def change_requests(self, limit: int = 50, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM inverter_change_requests"
        params: list[Any] = []
        if statuses:
            query += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            return [self._change_request(row) for row in connection.execute(query, params)]

    def change_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inverter_change_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return self._change_request(row) if row else None

    def decide_change_request(self, request_id: int, status: str, decided_by: str, decided_at: str) -> bool:
        """Move a request out of 'pending'. Returns False when it was already
        decided, so an approval cannot be replayed."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE inverter_change_requests SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, decided_by, decided_at, request_id),
            )
            return cursor.rowcount > 0

    def claim_change_request(self, request_id: int) -> bool:
        """Take an approved request for application exactly once."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE inverter_change_requests SET status = 'applying' "
                "WHERE id = ? AND status = 'approved'",
                (request_id,),
            )
            return cursor.rowcount > 0

    def finish_change_request(self, request_id: int, status: str, applied_at: str, result: dict) -> None:
        import json
        with self.connect() as connection:
            connection.execute(
                "UPDATE inverter_change_requests SET status = ?, applied_at = ?, result_json = ? WHERE id = ?",
                (status, applied_at, json.dumps(result, sort_keys=True, default=str), request_id),
            )

    def expire_change_requests(self, older_than_iso: str) -> int:
        """A stale proposal must not be approved much later against a baseline
        that has since moved."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE inverter_change_requests SET status = 'expired' "
                "WHERE status = 'pending' AND created_at < ?",
                (older_than_iso,),
            )
            return cursor.rowcount

    def latest_sample(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM samples ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def history(self, since_iso: str, max_points: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if not max_points:
                rows = connection.execute(
                    "SELECT * FROM samples WHERE captured_at >= ? ORDER BY captured_at", (since_iso,)
                ).fetchall()
            else:
                total = int(connection.execute(
                    "SELECT COUNT(*) FROM samples WHERE captured_at >= ?", (since_iso,)
                ).fetchone()[0])
                if total <= max_points:
                    rows = connection.execute(
                        "SELECT * FROM samples WHERE captured_at >= ? ORDER BY captured_at",
                        (since_iso,),
                    ).fetchall()
                else:
                    # Evenly sample the requested window and always retain its
                    # first and newest readings. This keeps large 7/30-day
                    # responses phone-sized while the complete decoded series
                    # remains available in SQLite and in the CSV export.
                    stride = max(2, (total - 1 + max_points - 2) // (max_points - 1))
                    rows = connection.execute(
                        "WITH ordered AS ("
                        " SELECT samples.*, ROW_NUMBER() OVER (ORDER BY captured_at) AS row_num,"
                        " COUNT(*) OVER () AS total_rows"
                        " FROM samples WHERE captured_at >= ?"
                        ") SELECT * FROM ordered WHERE row_num = 1 OR row_num = total_rows"
                        " OR ((row_num - 1) % ?) = 0 ORDER BY captured_at",
                        (since_iso, stride),
                    ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("row_num", None)
            item.pop("total_rows", None)
            result.append(item)
        return result

    @staticmethod
    def _diagnostic_frame_sql() -> str:
        return "(" + " OR ".join((
            "COALESCE(TRIM(error), '') <> ''",
            "LOWER(COALESCE(decode_note, '')) LIKE '%crc%'",
            "LOWER(COALESCE(decode_note, '')) LIKE '%unknown%'",
            "LOWER(COALESCE(decode_note, '')) LIKE '%not decoded%'",
            "LOWER(COALESCE(decode_note, '')) LIKE '%invalid%'",
            "LOWER(COALESCE(decode_note, '')) LIKE '%fault%'",
        )) + ")"

    def cleanup_retention(
        self,
        success_days: int = 7,
        diagnostic_days: int = 90,
        reference_time=None,
    ) -> dict[str, int]:
        """Prune bulky protocol captures without deleting decoded samples."""
        from datetime import datetime, timedelta, timezone

        success_days = max(1, int(success_days))
        diagnostic_days = max(success_days, int(diagnostic_days))
        reference_time = reference_time or datetime.now(timezone.utc)
        normal_cutoff = (reference_time - timedelta(days=success_days)).isoformat()
        diagnostic_cutoff = (reference_time - timedelta(days=diagnostic_days)).isoformat()
        diagnostic_sql = self._diagnostic_frame_sql()
        with self.connect() as connection:
            before_raw = int(connection.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0])
            before_config = int(connection.execute(
                "SELECT COUNT(*) FROM inverter_config_snapshots"
            ).fetchone()[0])
            connection.execute(
                f"DELETE FROM raw_frames WHERE captured_at < ? "
                f"AND (captured_at < ? OR NOT {diagnostic_sql})",
                (normal_cutoff, diagnostic_cutoff),
            )
            # Configuration snapshots are also bulky and mostly repetitive;
            # keeping the same long diagnostic window is ample for comparison.
            connection.execute(
                "DELETE FROM inverter_config_snapshots WHERE captured_at < ?",
                (diagnostic_cutoff,),
            )
            after_raw = int(connection.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0])
            after_config = int(connection.execute(
                "SELECT COUNT(*) FROM inverter_config_snapshots"
            ).fetchone()[0])
        return {
            "raw_frames_deleted": before_raw - after_raw,
            "config_snapshots_deleted": before_config - after_config,
        }

    def storage_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "samples": int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]),
                "raw_frames": int(connection.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0]),
                "config_snapshots": int(connection.execute(
                    "SELECT COUNT(*) FROM inverter_config_snapshots"
                ).fetchone()[0]),
            }

    def daily_summary(self, since_iso: str) -> list[dict[str, Any]]:
        # captured_at is ISO8601 UTC ("2026-09-02T19:52:30+00:00"); the first
        # 10 characters group samples by UTC calendar day without a separate
        # date column. avg_watts * 24h is a rough kWh estimate, not a
        # trapezoidal integral -- good enough for a dashboard summary, not
        # for billing.
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT substr(captured_at, 1, 10) AS day, "
                "AVG(pv_watts) AS avg_pv_watts, MAX(pv_watts) AS peak_pv_watts, "
                "AVG(ac_load_watts) AS avg_load_watts, MAX(ac_load_watts) AS peak_load_watts, "
                "MIN(battery_voltage) AS min_battery_voltage, COUNT(*) AS samples "
                "FROM samples WHERE captured_at >= ? GROUP BY day ORDER BY day",
                (since_iso,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fault_history(self, since_iso: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT captured_at, faults, operating_state FROM samples "
                "WHERE captured_at >= ? AND faults IS NOT NULL "
                "AND lower(trim(faults)) NOT IN ('', 'none', 'no fault', 'no faults', 'normal', '0') "
                "ORDER BY captured_at DESC LIMIT ?",
                (since_iso, limit),
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

    def pin_admin(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE pin_hash IS NOT NULL ORDER BY id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def set_pin(self, username: str, pin_hash: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE admin_users SET pin_hash = ?, pin_failures = 0, pin_locked = 0 "
                "WHERE username = ?",
                (pin_hash, username),
            )

    def record_pin_failure(self, username: str) -> tuple[int, bool]:
        with self.connect() as connection:
            connection.execute(
                "UPDATE admin_users SET pin_failures = pin_failures + 1 WHERE username = ?",
                (username,),
            )
            row = connection.execute(
                "SELECT pin_failures FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
            failures = int(row[0])
            locked = failures >= 3
            if locked:
                connection.execute(
                    "UPDATE admin_users SET pin_locked = 1 WHERE username = ?", (username,)
                )
        return failures, locked

    def reset_pin_failures(self, username: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE admin_users SET pin_failures = 0, pin_locked = 0 WHERE username = ?",
                (username,),
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
