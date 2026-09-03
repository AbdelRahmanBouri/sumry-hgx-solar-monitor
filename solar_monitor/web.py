from __future__ import annotations

from datetime import datetime, timedelta, timezone
import csv
from functools import wraps
import io
import json
from pathlib import Path
import secrets
import shutil
import time

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import privileged, system_info
from .collector import Collector
from .config import SerialConfig
from .db import Database
from .protocols import create_protocol
from .security import load_or_create_secret
from .serial_io import discover_usb_serial_ports, escaped_ascii, exchange

MANUAL_REFRESH_COOLDOWN_SECONDS = 5.0


def create_app(
    database: Database,
    serial_config: SerialConfig | None = None,
    collector: Collector | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database
    serial_config = serial_config or SerialConfig()
    private_dir = database.path.parent
    app.secret_key = load_or_create_secret(private_dir, "flask-secret")
    setup_token = load_or_create_secret(private_dir, "setup-token", 24)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    defaults = {
        "serial_port": serial_config.port,
        "baudrate": str(serial_config.baudrate),
        "timeout_seconds": str(serial_config.timeout_seconds),
        "poll_seconds": str(max(10, serial_config.poll_seconds)),
        "protocol": serial_config.protocol,
        "writes_enabled": "false",
    }
    existing = database.get_settings()
    missing = {key: value for key, value in defaults.items() if key not in existing}
    if missing:
        database.set_settings(missing, now())
    if collector is not None:
        try:
            collector.set_poll_seconds(int(database.get_settings().get("poll_seconds", "30")))
        except ValueError:
            collector.set_poll_seconds(30)

    def login_required(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not session.get("admin_user"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("admin_login", next=request.path))
            return function(*args, **kwargs)
        return wrapped

    def audit(action: str, detail: str = "") -> None:
        database.audit(now(), session.get("admin_user"), action, detail[:1000])

    def public_sample(sample):
        if sample is None:
            return None
        # This installation has neither a BMS nor a battery-temperature probe.
        # Keep legacy/raw channels in SQLite for diagnostics, but never imply
        # that an unconnected BTS input is a measured battery temperature.
        hidden = {"battery_temperature", "bts_temperature"}
        return {key: value for key, value in sample.items() if key not in hidden}

    @app.get("/")
    def dashboard():
        return render_template("index.html")

    @app.get("/lab")
    def dashboard_lab():
        return render_template("lab.html")

    @app.get("/api/status")
    def status():
        collector_state = (
            collector.state()
            if collector is not None
            else {"running": False, "reason": "collector is disabled"}
        )
        return jsonify({
            "sample": public_sample(database.latest_sample()),
            "inverter_config": database.latest_inverter_config(),
            "diagnostic": database.latest_diagnostic(),
            "collector": collector_state,
            "read_only": True,
        })

    last_manual_refresh = {"at": 0.0}

    @app.post("/api/refresh")
    def refresh():
        if collector is None:
            return jsonify({"error": "The collector is disabled on this instance"}), 503
        remaining = MANUAL_REFRESH_COOLDOWN_SECONDS - (time.monotonic() - last_manual_refresh["at"])
        if remaining > 0:
            return jsonify({
                "error": "Refresh is on cooldown",
                "retry_after_seconds": round(remaining, 1),
            }), 429
        last_manual_refresh["at"] = time.monotonic()
        collector.collect_once()
        return jsonify({
            "sample": public_sample(database.latest_sample()),
            "inverter_config": database.latest_inverter_config(),
            "diagnostic": database.latest_diagnostic(),
            "collector": collector.state(),
            "read_only": True,
        })

    def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(request.args.get(name, default))))
        except (TypeError, ValueError):
            return default

    @app.get("/api/history")
    def history():
        hours = _bounded_int("hours", 24, 1, 24 * 30)
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return jsonify([public_sample(sample) for sample in database.history(since.isoformat())])

    @app.get("/api/history/daily")
    def history_daily():
        days = _bounded_int("days", 30, 1, 90)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return jsonify(database.daily_summary(since.isoformat()))

    @app.get("/api/history/faults")
    def history_faults():
        days = _bounded_int("days", 30, 1, 90)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return jsonify(database.fault_history(since.isoformat()))

    @app.get("/api/system")
    def system_public():
        return jsonify(system_info.public_snapshot())

    @app.get("/health")
    def health():
        collector_state = (
            collector.state()
            if collector is not None
            else {"running": False, "reason": "collector is disabled"}
        )
        return jsonify({
            "ok": collector is None or collector_state["running"],
            "read_only": True,
            "collector_enabled": collector is not None,
            "collector_running": collector_state["running"],
            "collector": collector_state,
        })

    @app.route("/admin/setup", methods=["GET", "POST"])
    def admin_setup():
        if database.admin_count():
            return redirect(url_for("admin_login"))
        supplied = request.values.get("token", "")
        if not secrets.compare_digest(supplied, setup_token):
            abort(404)
        error = None
        if request.method == "POST":
            username = request.form.get("username", "admin").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirmation", "")
            if not (3 <= len(username) <= 40 and username.replace("-", "").replace("_", "").isalnum()):
                error = "Use 3–40 letters, numbers, underscores, or hyphens."
            elif len(password) < 10:
                error = "Use a password at least 10 characters long."
            elif password != confirmation:
                error = "Passwords do not match."
            else:
                database.create_admin(
                    username, generate_password_hash(password, method="pbkdf2:sha256:600000"), now()
                )
                session.clear()
                session["admin_user"] = username
                session["auth_method"] = "password"
                session.permanent = True
                audit("admin.created")
                return redirect(url_for("admin_home"))
        return render_template("admin_setup.html", token=supplied, error=error)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not database.admin_count():
            return render_template("admin_unconfigured.html")
        error = None
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = database.admin_by_username(username)
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["admin_user"] = username
                session["auth_method"] = "password"
                session.permanent = True
                database.record_login(username, now())
                database.reset_pin_failures(username)
                audit("auth.login")
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/admin") else url_for("admin_home"))
            database.audit(now(), username or None, "auth.failed", request.remote_addr or "")
            error = "Incorrect username or password."
        pin_admin = database.pin_admin()
        return render_template(
            "admin_login.html", error=error,
            pin_available=bool(pin_admin and not pin_admin["pin_locked"]),
            pin_locked=bool(pin_admin and pin_admin["pin_locked"]),
            pin_error=request.args.get("pin_error"),
        )

    @app.post("/admin/pin-login")
    def admin_pin_login():
        user = database.pin_admin()
        pin = request.form.get("pin", "")
        if not user or user["pin_locked"]:
            return redirect(url_for("admin_login"))
        if pin.isdigit() and check_password_hash(user["pin_hash"], pin):
            session.clear()
            session["admin_user"] = user["username"]
            session["auth_method"] = "pin"
            session.permanent = True
            database.reset_pin_failures(user["username"])
            database.record_login(user["username"], now())
            audit("auth.pin_login")
            return redirect(url_for("admin_home"))
        failures, locked = database.record_pin_failure(user["username"])
        database.audit(
            now(), user["username"], "auth.pin_failed",
            "PIN locked; full password required" if locked else f"attempt {failures} of 3",
        )
        return redirect(url_for("admin_login", pin_error="locked" if locked else "invalid"))

    @app.post("/admin/logout")
    @login_required
    def admin_logout():
        audit("auth.logout")
        session.clear()
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @login_required
    def admin_home():
        return render_template("admin.html", username=session["admin_user"])

    @app.get("/api/admin/overview")
    @login_required
    def admin_overview():
        disk = shutil.disk_usage(database.path.parent)
        return jsonify({
            "ports": discover_usb_serial_ports(),
            "settings": database.get_settings(),
            "latest_diagnostic": database.latest_diagnostic(),
            "database_bytes": database.path.stat().st_size if database.path.exists() else 0,
            "disk_free_bytes": disk.free,
            "uptime_seconds": system_info.uptime_seconds(),
            "writes_enabled": database.get_settings().get("writes_enabled") == "true",
            "system": system_info.admin_snapshot(),
        })

    @app.post("/api/admin/system/reboot")
    @login_required
    def system_reboot():
        ok, message = privileged.reboot()
        audit("system.reboot", "requested" if ok else message)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.post("/api/admin/system/restart-service")
    @login_required
    def system_restart_service():
        ok, message = privileged.restart_service()
        audit("system.restart_service", "requested" if ok else message)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.route("/api/admin/system/eco", methods=["GET", "POST"])
    @login_required
    def system_eco():
        if request.method == "GET":
            return jsonify({"governor": system_info.governor()})
        payload = request.get_json(silent=True) or {}
        mode = "powersave" if payload.get("enabled") else "ondemand"
        ok, message = privileged.set_governor(mode)
        audit("system.eco_mode", f"{mode}: {'applied' if ok else message}")
        return jsonify({
            "ok": ok, "message": message,
            "governor": system_info.governor() if ok else None,
        }), (200 if ok else 400)

    @app.get("/api/admin/system/wifi")
    @login_required
    def system_wifi_scan():
        ok, message = privileged.wifi_scan()
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"networks": privileged.parse_wifi_scan(message)})

    @app.post("/api/admin/system/wifi")
    @login_required
    def system_wifi_connect():
        payload = request.get_json(silent=True) or {}
        ssid = str(payload.get("ssid", "")).strip()
        password = str(payload.get("password", ""))
        if not ssid or len(ssid) > 32:
            return jsonify({"error": "Enter a valid network name"}), 400
        ok, message = privileged.wifi_connect(ssid, password)
        audit("system.wifi_connect", f"{ssid}: {'ok' if ok else message}")
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.post("/api/admin/system/update-check")
    @login_required
    def system_update_check():
        result = system_info.check_updates(Path(__file__).resolve().parent.parent)
        audit("system.update_check")
        return jsonify(result)

    @app.get("/api/admin/frames")
    @login_required
    def admin_frames():
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        return jsonify(database.raw_frames(limit))

    @app.get("/api/admin/audit")
    @login_required
    def admin_audit():
        return jsonify(database.recent_audit(100))

    @app.route("/api/admin/security", methods=["GET", "POST"])
    @login_required
    def admin_security():
        username = session["admin_user"]
        user = database.admin_by_username(username)
        if request.method == "GET":
            return jsonify({
                "pin_enabled": bool(user and user["pin_hash"]),
                "pin_locked": bool(user and user["pin_locked"]),
                "pin_failures": int(user["pin_failures"] if user else 0),
                "auth_method": session.get("auth_method", "password"),
            })
        if session.get("auth_method") != "password":
            return jsonify({"error": "Sign in with your full password to change the PIN"}), 403
        payload = request.get_json(silent=True) or {}
        action = payload.get("action")
        if action == "disable":
            database.set_pin(username, None)
            audit("security.pin_disabled")
        elif action == "set":
            pin = str(payload.get("pin", ""))
            if not (4 <= len(pin) <= 8 and pin.isdigit()):
                return jsonify({"error": "PIN must contain 4–8 digits"}), 400
            database.set_pin(
                username, generate_password_hash(pin, method="pbkdf2:sha256:600000")
            )
            audit("security.pin_set")
        else:
            return jsonify({"error": "Unknown action"}), 400
        user = database.admin_by_username(username)
        return jsonify({"pin_enabled": bool(user["pin_hash"]), "pin_locked": False,
                        "pin_failures": 0, "auth_method": session.get("auth_method")})

    @app.route("/api/admin/settings", methods=["GET", "POST"])
    @login_required
    def admin_settings():
        if request.method == "GET":
            return jsonify(database.get_settings())
        payload = request.get_json(silent=True) or {}
        allowed = {"serial_port", "baudrate", "timeout_seconds", "poll_seconds", "protocol"}
        values = {key: str(value).strip() for key, value in payload.items() if key in allowed}
        try:
            if "baudrate" in values and int(values["baudrate"]) not in {1200, 2400, 4800, 9600, 19200}:
                raise ValueError("Unsupported baud rate")
            if "timeout_seconds" in values and not 0.1 <= float(values["timeout_seconds"]) <= 10:
                raise ValueError("Timeout must be between 0.1 and 10 seconds")
            if "poll_seconds" in values and not 10 <= int(values["poll_seconds"]) <= 3600:
                raise ValueError("Polling must be between 10 and 3600 seconds")
            if "protocol" in values:
                create_protocol(values["protocol"])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        database.set_settings(values, now())
        if collector is not None and "poll_seconds" in values:
            collector.set_poll_seconds(int(values["poll_seconds"]))
        audit("settings.updated", json.dumps(values, sort_keys=True))
        return jsonify(database.get_settings())

    @app.post("/api/admin/serial/<mode>")
    @login_required
    def admin_serial(mode: str):
        if mode not in {"listen", "probe"}:
            abort(404)
        settings = database.get_settings()
        ports = discover_usb_serial_ports()
        port = settings.get("serial_port", "auto")
        if port == "auto":
            port = ports[0] if ports else ""
        if not port:
            return jsonify({"error": "No USB serial adapter detected"}), 400
        baudrate = int(settings.get("baudrate", "9600"))
        timeout = float(settings.get("timeout_seconds", "1"))
        protocol = create_protocol(settings.get("protocol", "sumry_8851_probe"))
        if mode == "listen":
            try:
                import serial
                with serial.Serial(port, baudrate, timeout=timeout) as connection:
                    response = connection.read(4096)
                record = {"captured_at": now(), "port": port, "baudrate": baudrate,
                          "protocol": protocol.name, "query_name": "listen_only",
                          "request_hex": "", "response_hex": response.hex(" ").upper(),
                          "response_ascii": escaped_ascii(response), "elapsed_ms": int(timeout * 1000),
                          "decode_note": "Passive listen-only capture", "error": None}
            except Exception as exc:
                return jsonify({"error": str(exc)}), 400
        else:
            query = protocol.queries[0]
            result = exchange(port, baudrate, timeout, protocol, query)
            decoded = protocol.decode(result.response)
            record = {"captured_at": result.timestamp, "port": port, "baudrate": baudrate,
                      "protocol": protocol.name, "query_name": result.query_name,
                      "request_hex": result.request.hex(" ").upper(),
                      "response_hex": result.response.hex(" ").upper(),
                      "response_ascii": escaped_ascii(result.response), "elapsed_ms": result.elapsed_ms,
                      "decode_note": decoded.note, "error": result.error}
        database.save_raw_frame(record)
        audit(f"serial.{mode}", f"{port} @ {baudrate}")
        return jsonify(record), (400 if record.get("error") else 200)

    @app.get("/api/admin/export/frames.jsonl")
    @login_required
    def export_frames():
        rows = reversed(database.raw_frames(500))
        body = "".join(json.dumps(row, default=str) + "\n" for row in rows)
        audit("data.export", "frames.jsonl")
        return Response(body, mimetype="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=solar-frames.jsonl"})

    @app.get("/api/admin/export/samples.csv")
    @login_required
    def export_samples():
        rows = database.history("1970-01-01T00:00:00+00:00")
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        audit("data.export", "samples.csv")
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=solar-samples.csv"})

    return app


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
