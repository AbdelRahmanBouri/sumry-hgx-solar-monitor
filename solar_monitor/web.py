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
from . import solar_estimate

MANUAL_REFRESH_COOLDOWN_SECONDS = 5.0
FORECAST_PREVIEW = timedelta(hours=6)


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
        "raw_success_days": "7",
        "raw_diagnostic_days": "90",
        "history_max_points": "1500",
        # This installation's site and confirmed array nameplate. There are
        # eight SunPower SPR-E20-330-COM modules wired 4S2P: 2.64 kW DC,
        # 218.8 Vmp, 259.6 Voc and 12.08 A Imp.
        #
        # The panels are flush-mounted on both slopes of the tiny home's
        # pitched roof, four per slope. That placement comes from the owner,
        # not from the wiring: 4S2P describes how the modules are connected
        # and says nothing about where they sit. `dual_slope` models the split.
        #
        # The slopes face north and south (azimuth 5 and 185), read off the
        # roofline rather than the logged data: the site photos looking north
        # (camera heading 0.6 deg true) show the home's wide south face under
        # a horizontal eave, with only a decorative pediment over the door.
        # A gable end would read as one full triangle across that face; a
        # level eave means the ridge runs east-west behind it, so the roof
        # falls toward and away from the camera. The site grid agrees -- the
        # block walls run north-south and the street runs east-west along the
        # south edge of the north-up satellite view.
        #
        # Consequence worth remembering: four of the eight modules face north.
        #
        # And the strings straddle the ridge -- each 4-module series string
        # carries two modules on each face, rather than one string per face.
        # Series modules must share a current, so the shaded pair pins what
        # the sunlit pair may deliver: a few percent in summer, but around a
        # third at the winter solstice. `split_strings` models that. Re-landing
        # the strings one-per-face is a wiring change, not a hardware one, and
        # would remove the loss outright.
        #
        # Two faces pointing opposite ways very nearly cancel, so a symmetric
        # gable collects within 1-2% of a flat array of the same size no
        # matter the pitch or which way the ridge runs. The exact tilt here
        # is therefore a low-stakes assumption (a typical 3:12 tiny-home
        # pitch); the performance ratio is what actually sets the level, and
        # 0.74 is what this system's own production implies against its known
        # 2,640 W nameplate.
        #
        # Do not be tempted to "fit" a steep single plane to the logged data:
        # battery-full curtailment removes every midday sample, leaving only
        # morning and evening, and low-sun-only data always prefers a steep
        # tilt. That artefact is what these values replace.
        "estimator_latitude": "34.0522",
        "estimator_longitude": "-118.2437",
        "estimator_capacity_watts": "2640",
        "estimator_performance_ratio": "0.74",
        "estimator_tilt_degrees": "14",
        "estimator_azimuth_degrees": "185",
        "estimator_dual_slope": "true",
        "estimator_split_strings": "true",
    }
    existing = database.get_settings()
    missing = {key: value for key, value in defaults.items() if key not in existing}
    # An earlier release stored the estimator keys as empty strings, so a blank
    # value is also backfilled -- an admin who clears a field on purpose can
    # still leave the estimator switched off by zeroing the array capacity.
    missing.update({
        key: value for key, value in defaults.items()
        if key.startswith("estimator_") and value and not existing.get(key, "").strip()
    })
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

    @app.get("/estimate")
    @app.get("/solar-potential")
    def solar_potential():
        return render_template("estimate.html")

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
        settings = database.get_settings()
        max_points = _bounded_int(
            "points", int(settings.get("history_max_points", "1500")), 250, 5000
        )
        rows = database.history(since.isoformat(), max_points=max_points)
        response = jsonify([public_sample(sample) for sample in rows])
        response.headers["X-History-Points"] = str(len(rows))
        return response

    @app.get("/api/history/daily")
    def history_daily():
        days = _bounded_int("days", 30, 1, 90)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return jsonify(database.daily_summary(since.isoformat()))

    @app.get("/api/estimate")
    def estimate():
        unconfigured = {
            "configured": False,
            "location": None,
            "capacity_watts": 0,
            "performance_ratio": 0,
            "weather_source": None,
            "points": [],
        }
        settings = database.get_settings()
        try:
            # Latitude/longitude are blank until an admin sets them, which
            # raises here and reports the estimator as simply not configured.
            lat = float(settings["estimator_latitude"])
            lon = float(settings["estimator_longitude"])
            capacity_watts = float(settings.get("estimator_capacity_watts") or 0)
            performance_ratio = float(settings.get("estimator_performance_ratio") or 0.8)
            # Blank tilt/azimuth means "orientation unknown"; the model then
            # stays on the horizontal plane instead of inventing an angle.
            tilt = settings.get("estimator_tilt_degrees", "").strip()
            azimuth = settings.get("estimator_azimuth_degrees", "").strip()
            tilt_degrees = float(tilt) if tilt else None
            azimuth_degrees = float(azimuth) if azimuth else None
            dual_slope = settings.get("estimator_dual_slope", "false") == "true"
            split_strings = settings.get("estimator_split_strings", "false") == "true"
        except (KeyError, ValueError):
            return jsonify(unconfigured)
        if capacity_watts <= 0:
            return jsonify(unconfigured)

        # `hours` is the look-back window; the next few hours are always
        # included as well (each point says whether it is a forecast) so a
        # caller can show what is still to come without a second request.
        hours = _bounded_int("hours", 24, 1, 48)
        now_utc = datetime.now(timezone.utc)
        window = (now_utc - timedelta(hours=hours), now_utc + FORECAST_PREVIEW)
        points, weather_source = solar_estimate.estimate_series(
            lat, lon, capacity_watts, performance_ratio, *window,
            tilt_degrees=tilt_degrees, azimuth_degrees=azimuth_degrees,
            dual_slope=dual_slope, split_strings=split_strings,
        )

        # What the same panels would collect after the two losses that cost
        # nothing but labour to undo: re-landing each string on one face, and
        # then moving the shaded half onto the sunny slope. Both are modelled
        # over the identical window so the comparison is like for like.
        def window_kwh(series):
            if len(series) < 2:
                return 0.0
            step_hours = (
                datetime.fromisoformat(series[1]["timestamp"])
                - datetime.fromisoformat(series[0]["timestamp"])
            ).total_seconds() / 3600
            return sum(point["estimated_watts"] for point in series) * step_hours / 1000

        scenarios = {"as_built_kwh": round(window_kwh(points), 2)}
        if dual_slope and tilt_degrees is not None and azimuth_degrees is not None:
            per_face, _ = solar_estimate.estimate_series(
                lat, lon, capacity_watts, performance_ratio, *window,
                tilt_degrees=tilt_degrees, azimuth_degrees=azimuth_degrees,
                dual_slope=True, split_strings=False,
            )
            one_plane, _ = solar_estimate.estimate_series(
                lat, lon, capacity_watts, performance_ratio, *window,
                tilt_degrees=tilt_degrees, azimuth_degrees=azimuth_degrees,
                dual_slope=False, split_strings=False,
            )
            scenarios.update({
                "strings_per_face_kwh": round(window_kwh(per_face), 2),
                "all_one_slope_kwh": round(window_kwh(one_plane), 2),
            })
        return jsonify({
            "configured": True,
            "location": {"latitude": lat, "longitude": lon},
            "capacity_watts": capacity_watts,
            "performance_ratio": performance_ratio,
            "array": {
                "module": "SunPower SPR-E20-330-COM",
                "module_count": 8,
                "module_watts": 330,
                "wiring": "4S2P",
                "vmp_volts": 218.8,
                "voc_volts": 259.6,
                "imp_amps": 12.08,
                "isc_amps": 13.04,
                "tilt_degrees": tilt_degrees,
                "azimuth_degrees": azimuth_degrees,
                "dual_slope": dual_slope,
                "split_strings": split_strings,
                "mounting": "flush on both roof slopes" if dual_slope else "single plane",
                "stringing": (
                    "each series string spans both faces" if split_strings and dual_slope
                    else "one series string per face" if dual_slope else "single plane"
                ),
            },
            "system": {
                "inverter": "SUMRY HGX 4KW 24V/120V",
                "battery_bank": "4 x Odyssey ODX-AGM31, 2S2P",
                "battery_nominal_volts": 24,
                "battery_capacity_ah": 196,
                "configured_charge_limit_amps": 80,
            },
            "weather_source": weather_source,
            "scenarios": scenarios,
            "points": points,
        })

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
            "storage_counts": database.storage_counts(),
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

    @app.post("/api/admin/data/cleanup")
    @login_required
    def admin_data_cleanup():
        settings = database.get_settings()
        result = database.cleanup_retention(
            int(settings.get("raw_success_days", "7")),
            int(settings.get("raw_diagnostic_days", "90")),
        )
        audit("data.cleanup", json.dumps(result, sort_keys=True))
        return jsonify({"ok": True, **result, "counts": database.storage_counts()})

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
        allowed = {
            "serial_port", "baudrate", "timeout_seconds", "poll_seconds", "protocol",
            "raw_success_days", "raw_diagnostic_days", "history_max_points",
            "estimator_latitude", "estimator_longitude",
            "estimator_capacity_watts", "estimator_performance_ratio",
            "estimator_tilt_degrees", "estimator_azimuth_degrees",
            "estimator_dual_slope", "estimator_split_strings",
        }
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
            if "raw_success_days" in values and not 1 <= int(values["raw_success_days"]) <= 30:
                raise ValueError("Routine frame retention must be between 1 and 30 days")
            if "raw_diagnostic_days" in values and not 7 <= int(values["raw_diagnostic_days"]) <= 365:
                raise ValueError("Diagnostic retention must be between 7 and 365 days")
            merged = {**database.get_settings(), **values}
            if int(merged.get("raw_diagnostic_days", "90")) < int(merged.get("raw_success_days", "7")):
                raise ValueError("Diagnostic retention cannot be shorter than routine retention")
            if "history_max_points" in values and not 250 <= int(values["history_max_points"]) <= 5000:
                raise ValueError("Chart point limit must be between 250 and 5000")
            if values.get("estimator_latitude") and not -90 <= float(values["estimator_latitude"]) <= 90:
                raise ValueError("Latitude must be between -90 and 90")
            if values.get("estimator_longitude") and not -180 <= float(values["estimator_longitude"]) <= 180:
                raise ValueError("Longitude must be between -180 and 180")
            # Zero is accepted and switches the estimator off for good; a
            # blank field instead means "never set" and is re-seeded on start.
            if values.get("estimator_capacity_watts") and not 0 <= float(values["estimator_capacity_watts"]) <= 100000:
                raise ValueError("Array capacity must be between 0 and 100,000 watts")
            if "estimator_performance_ratio" in values and not 0.1 <= float(values["estimator_performance_ratio"]) <= 1:
                raise ValueError("Performance ratio must be between 0.1 and 1.0")
            if values.get("estimator_tilt_degrees") and not 0 <= float(values["estimator_tilt_degrees"]) <= 90:
                raise ValueError("Array tilt must be between 0 and 90 degrees")
            if values.get("estimator_azimuth_degrees") and not 0 <= float(values["estimator_azimuth_degrees"]) <= 360:
                raise ValueError("Array azimuth must be between 0 and 360 degrees")
            if values.get("estimator_dual_slope") not in (None, "", "true", "false"):
                raise ValueError("Dual slope must be true or false")
            if values.get("estimator_split_strings") not in (None, "", "true", "false"):
                raise ValueError("String layout must be true or false")
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
