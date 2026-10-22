from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
import sqlite3
from typing import Any

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
    error TEXT,
    decoded INTEGER
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


# A routine frame is a read that came back and decoded; it only needs to
# outlive the short window. Everything else -- serial errors, CRC failures,
# replies that would not decode, writes, passive captures -- is a diagnostic
# and is kept for the longer one.
#
# Rows from before the `decoded` flag existed fall back to the one wording
# every decoded frame's note has always started with. The fallback is a
# prefix on purpose: this used to be a set of substrings searched anywhere
# in the note, and "not decoded" and "fault" both occur in the note of a
# perfectly good status frame, so no frame ever counted as routine and the
# short window never deleted anything.
# A retention window of 0 days keeps frames for good.
KEEP_FOREVER = 0

_ROUTINE_FRAME_SQL = (
    "(COALESCE(TRIM(error), '') = '' "
    "AND COALESCE(decoded, decode_note LIKE 'CRC-validated%', 0) = 1)"
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
        # Write-ahead logging lets the collector's inserts and the web
        # requests' reads proceed without waiting on each other. The mode is
        # stored in the file, so setting it once here covers every later
        # connection; setting it again is a no-op.
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")

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

        # Whether a frame decoded, as a flag rather than something inferred
        # from the note's wording. Rows written before it existed stay NULL
        # and are classified by `_ROUTINE_FRAME_SQL`'s fallback.
        frame_columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_frames)")}
        if "decoded" not in frame_columns:
            connection.execute("ALTER TABLE raw_frames ADD COLUMN decoded INTEGER")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        # Unlike the journal mode, synchronous is per connection. NORMAL under
        # WAL still survives a crash of the process; it only skips the fsync on
        # every commit, which matters for a database on an SD card.
        connection.execute("PRAGMA synchronous=NORMAL")
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
        self.save_samples([(captured_at, metrics)])

    def save_samples(self, readings: list[tuple[str, dict[str, Any]]]) -> None:
        """Store (captured_at, metrics) pairs in one transaction. Only the
        allow-listed metric columns are written."""
        with self.connect() as connection:
            for captured_at, metrics in readings:
                values = {key: value for key, value in metrics.items() if key in SAMPLE_METRIC_COLUMNS}
                values["captured_at"] = captured_at
                columns = ", ".join(values)
                placeholders = ", ".join(f":{key}" for key in values)
                connection.execute(f"INSERT INTO samples ({columns}) VALUES ({placeholders})", values)

    def save_inverter_config(self, captured_at: str, settings: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO inverter_config_snapshots (captured_at, config_json) VALUES (?, ?)",
                (captured_at, json.dumps(settings, sort_keys=True)),
            )

    def latest_inverter_config(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT captured_at, config_json FROM inverter_config_snapshots "
                "ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"captured_at": row["captured_at"], "settings": json.loads(row["config_json"])}

    def inverter_config_changes(self, limit: int = 30) -> list[dict[str, Any]]:
        """One snapshot per configuration the inverter has held, newest first:
        the first snapshot of each run of identical ones, i.e. when that
        configuration took effect. The collector snapshots the block every
        few minutes, so the last thirty snapshots are usually thirty copies
        of one configuration -- no use as a list of points to restore."""
        with self.connect() as connection:
            rows = connection.execute(
                "WITH ordered AS ("
                " SELECT id, captured_at, config_json,"
                " LAG(config_json) OVER (ORDER BY captured_at) AS previous"
                " FROM inverter_config_snapshots"
                ") SELECT id, captured_at, config_json FROM ordered"
                " WHERE previous IS NULL OR previous <> config_json"
                " ORDER BY captured_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": row["id"], "captured_at": row["captured_at"], "settings": json.loads(row["config_json"])}
            for row in rows
        ]

    def inverter_config_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
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
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO inverter_change_requests "
                "(created_at, requested_by, reason, changes_json, status, decided_by, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, requested_by, reason, json.dumps(changes, sort_keys=True),
                 status, decided_by, decided_at),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    @staticmethod
    def _change_request(row: sqlite3.Row) -> dict[str, Any]:
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

    def finish_change_request(self, request_id: int, status: str, applied_at: str, result: dict[str, Any]) -> None:
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
                "WHERE status IN ('pending', 'approved') AND created_at < ?",
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

    def cleanup_retention(
        self,
        success_days: int = 7,
        diagnostic_days: int = 90,
        reference_time: datetime | None = None,
    ) -> dict[str, int]:
        """Prune raw protocol captures. Either window may be KEEP_FOREVER.

        Decoded samples and configuration snapshots are never deleted: they
        are the history itself. A raw frame holds what the decoder does not
        read yet -- the operating-state and fault bytes, unidentified words --
        so keeping them lets a later decoder be run over the whole past, at
        about 1 KB a poll (~1 GB a year at 30 s).
        """
        success_days, diagnostic_days = int(success_days), int(diagnostic_days)
        if success_days <= KEEP_FOREVER:
            # Diagnostics are never kept for less time than routine frames.
            return {"raw_frames_deleted": 0}
        reference_time = reference_time or datetime.now(UTC)
        normal_cutoff = (reference_time - timedelta(days=success_days)).isoformat()
        parameters: tuple[str, ...]
        if diagnostic_days <= KEEP_FOREVER:
            condition, parameters = _ROUTINE_FRAME_SQL, (normal_cutoff,)
        else:
            diagnostic_cutoff = (reference_time - timedelta(days=max(success_days, diagnostic_days))).isoformat()
            condition, parameters = f"(captured_at < ? OR {_ROUTINE_FRAME_SQL})", (normal_cutoff, diagnostic_cutoff)
        with self.connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM raw_frames WHERE captured_at < ? AND {condition}", parameters
            ).rowcount
        return {"raw_frames_deleted": deleted}

    def storage_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "samples": int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]),
                "raw_frames": int(connection.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0]),
                "config_snapshots": int(connection.execute(
                    "SELECT COUNT(*) FROM inverter_config_snapshots"
                ).fetchone()[0]),
            }

    def daily_summary(
        self, today: str, days: int, utc_offset_minutes: int | None = 0, *, local_timezone: tzinfo | None = None,
    ) -> list[dict[str, Any]]:
        """The last `days` local calendar days ending on `today`, oldest first.

        `today` is a local date ("2026-09-16") and `utc_offset_minutes` the
        local clock's offset from UTC; None uses the server's historical local
        timezone rules. An explicit local_timezone overrides the fixed offset.
        captured_at is stored in UTC. Every day in the window comes back, zeroed where it has no samples, so a
        caller can sum a true window rather than the last N days that
        happened to have data.

        Energy is the trapezoid between consecutive readings with the gap
        capped at five minutes, as in the dashboard's energy integrator.
        coverage_hours is the sum of those capped gaps. Intervals crossing midnight are split between the
        two days, interpolating power at the boundary. A day with thirty minutes
        of readings reports thirty minutes and the energy in them, not a whole day
        extrapolated from its average.
        """
        max_gap_hours = 5 / 60
        zone = local_timezone
        if zone is None and utc_offset_minutes is not None:
            zone = timezone(timedelta(minutes=utc_offset_minutes))
        first = datetime.fromisoformat(today) - timedelta(days=max(1, int(days)) - 1)
        calendar = []
        parameters: dict[str, str | float] = {"max_gap": max_gap_hours}
        for index in range(max(1, int(days))):
            day = first + timedelta(days=index)
            # Convert each local midnight separately: a civil day may be 23 or
            # 25 hours. Naive astimezone uses the system's rules for that date.
            start = day.replace(tzinfo=zone).astimezone(UTC)
            end = (day + timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
            parameters.update({f"day{index}": day.date().isoformat(),
                               f"start{index}": start.isoformat(), f"end{index}": end.isoformat()})
            calendar.append(f"(:day{index}, julianday(:start{index}), julianday(:end{index}))")
            if index == 0:
                parameters["scan_start"] = start.strftime("%Y-%m-%dT%H:%M:%S")
        parameters["scan_end"] = (end + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.connect() as connection:
            rows = connection.execute(
                "WITH calendar(day, start_day, end_day) AS (VALUES " + ",".join(calendar) + ")," +
                """
                stepped AS (
                    SELECT julianday(captured_at) AS end_day,
                        julianday(LAG(captured_at) OVER w) AS previous_day,
                        pv_watts, ac_load_watts, battery_voltage,
                        LAG(pv_watts) OVER w AS previous_pv,
                        LAG(ac_load_watts) OVER w AS previous_load
                    FROM samples
                    WHERE captured_at >= COALESCE(
                        (SELECT MAX(captured_at) FROM samples
                         WHERE captured_at < :scan_start),
                        :scan_start)
                        AND captured_at < :scan_end
                    WINDOW w AS (ORDER BY captured_at)
                ),
                intervals AS (
                    SELECT *, MAX(previous_day, end_day - :max_gap / 24) AS start_day
                    FROM stepped WHERE previous_day IS NOT NULL AND end_day > previous_day
                ),
                overlaps AS (
                    SELECT calendar.day, intervals.*,
                        MAX(intervals.start_day, calendar.start_day) AS left_day,
                        MIN(intervals.end_day, calendar.end_day) AS right_day
                    FROM calendar JOIN intervals
                        ON intervals.end_day > calendar.start_day AND intervals.start_day < calendar.end_day
                ),
                slices AS (
                    SELECT *, (right_day - left_day) * 24 AS hours,
                        ((left_day - start_day) + (right_day - start_day))
                            / (2 * (end_day - start_day)) AS midpoint FROM overlaps
                ),
                energy AS (
                    SELECT day, SUM(hours) AS coverage_hours,
                        SUM((previous_pv + (pv_watts - previous_pv) * midpoint) * hours) / 1000 AS pv_kwh,
                        SUM((previous_load + (ac_load_watts - previous_load) * midpoint) * hours) / 1000 AS load_kwh
                    FROM slices GROUP BY day
                ),
                statistics AS (
                    SELECT day, COUNT(*) AS samples,
                        AVG(pv_watts) AS avg_pv_watts, MAX(pv_watts) AS peak_pv_watts,
                        AVG(ac_load_watts) AS avg_load_watts, MAX(ac_load_watts) AS peak_load_watts,
                        MIN(battery_voltage) AS min_battery_voltage
                    FROM calendar JOIN stepped
                        ON stepped.end_day >= calendar.start_day AND stepped.end_day < calendar.end_day
                    GROUP BY day
                )
                SELECT calendar.day, COALESCE(samples, 0) AS samples,
                    ROUND(COALESCE(coverage_hours, 0), 2) AS coverage_hours,
                    ROUND(COALESCE(pv_kwh, 0), 3) AS pv_kwh,
                    ROUND(COALESCE(load_kwh, 0), 3) AS load_kwh,
                    avg_pv_watts, peak_pv_watts, avg_load_watts, peak_load_watts, min_battery_voltage
                FROM calendar LEFT JOIN statistics USING (day) LEFT JOIN energy USING (day)
                ORDER BY calendar.day
                """,
                parameters,
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
