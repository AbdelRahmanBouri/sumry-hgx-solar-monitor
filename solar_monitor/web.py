from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
import csv
from datetime import UTC, datetime, timedelta
from functools import wraps
import io
import json
import logging
from pathlib import Path
import secrets
import shutil
import time
from typing import Any, ParamSpec, cast
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
import serial
from werkzeug.security import check_password_hash, generate_password_hash

from . import battery as battery_model
from . import change_policy, privileged, propose, solar_estimate, system_info
from . import utility as utility_model
from .collector import Collector
from .config import SerialConfig, SiteConfig
from .db import Database
from .protocols import create_protocol
from .security import load_or_create_secret
from .serial_io import Exchange, discover_usb_serial_ports, exchange

P = ParamSpec("P")

LOG = logging.getLogger(__name__)
MANUAL_REFRESH_COOLDOWN_SECONDS = 5.0
# What /api/history returns when a caller does not say; the dashboard and the
# potential page both ask for what they need.
DEFAULT_HISTORY_POINTS = 1500
PASSWORD_ATTEMPTS = 5
PASSWORD_LOCKOUT_SECONDS = 15 * 60
FORECAST_PREVIEW = timedelta(hours=6)


def create_app(
    database: Database,
    serial_config: SerialConfig | None = None,
    collector: Collector | None = None,
    *,
    site: SiteConfig | None = None,
    expose_network_info: bool = False,
) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database
    serial_config = serial_config or SerialConfig()
    private_dir = database.path.parent
    app.secret_key = load_or_create_secret(private_dir, "flask-secret")
    setup_token = load_or_create_secret(private_dir, "setup-token", 24)
    if not database.admin_count():
        # Where the token is, never the token itself: the journal is read
        # more widely than a 0600 file.
        LOG.info("No administrator yet: open /admin/setup?token=<token> with the token in %s",
                 private_dir / "setup-token")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    @app.before_request
    def reject_cross_origin_mutations() -> ResponseReturnValue | None:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin is not None:
                try:
                    source = urlsplit(origin)
                except ValueError:
                    return jsonify({"error": "Invalid request origin"}), 403
                if source.scheme not in {"http", "https"} or source.netloc != request.host:
                    return jsonify({"error": "Cross-origin changes are not allowed"}), 403
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify({"error": "Cross-site changes are not allowed"}), 403

        return None

    @app.after_request
    def response_security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith(("/admin", "/api/admin")):
            response.headers["Cache-Control"] = "no-store"
        return response

    # The serial link itself -- port, baud, timeout, protocol -- is not here:
    # the collector opens it from the config file, so a copy in the settings
    # table could only disagree with what is actually running.
    defaults = {
        "poll_seconds": str(max(10, serial_config.poll_seconds)),
        "writes_enabled": "false",
        "raw_success_days": "7",
        "raw_diagnostic_days": "90",
    }
    # The site profile seeds the estimator and the battery nameplate. Only the
    # config file knows them, so with no profile nothing site-specific is
    # written and the estimator stays off until an admin fills in the form.
    # The settings table is the working copy: an admin's edit outlives the
    # file's value on every later start.
    site_settings: dict[str, str] = {}
    if site is not None:
        site_settings = {
            key: str(value)
            for key, value in {
                "estimator_latitude": site.latitude,
                "estimator_longitude": site.longitude,
                "estimator_capacity_watts": site.array_capacity_watts,
                "estimator_performance_ratio": site.performance_ratio,
                "estimator_tilt_degrees": site.tilt_degrees,
                "estimator_azimuth_degrees": site.azimuth_degrees,
                "battery_capacity_ah": site.battery_capacity_ah,
            }.items()
            if value is not None
        }
        # An unset flag means a single plane with whole strings, which is
        # also what the estimator assumes when the key is absent.
        site_settings["estimator_dual_slope"] = "true" if site.dual_slope else "false"
        site_settings["estimator_split_strings"] = "true" if site.split_strings else "false"
    defaults.update(site_settings)
    existing = database.get_settings()
    missing = {key: value for key, value in defaults.items() if key not in existing}
    # An earlier release stored the estimator keys as empty strings, so a blank
    # value is also backfilled -- an admin who clears a field on purpose can
    # still leave the estimator switched off by zeroing the array capacity.
    missing.update({
        key: value for key, value in site_settings.items()
        if key.startswith("estimator_") and value and not existing.get(key, "").strip()
    })
    if missing:
        database.set_settings(missing, now())
    profile = site or SiteConfig()

    def battery_capacity_ah(settings: dict[str, str]) -> float | None:
        """The bank's nameplate: the admin's setting, else the site profile,
        else unknown. Never a number the installation did not supply."""
        for value in (settings.get("battery_capacity_ah"), profile.battery_capacity_ah):
            try:
                capacity = float(value or 0)
            except ValueError:
                continue
            if capacity > 0:
                return capacity
        return None
    if collector is not None:
        try:
            collector.set_poll_seconds(int(database.get_settings().get("poll_seconds", "30")))
        except ValueError:
            collector.set_poll_seconds(30)

    @app.template_global()
    def version(filename: str) -> str:
        """A cache-busting version for a static file, from when it last
        changed. Pages are rendered fresh but their assets are cached hard,
        so an update must change the URL of every script and stylesheet it
        touched -- otherwise a phone pairs the new page with the old script.
        A hand-kept counter had already drifted: the login pages asked for
        admin.css?v=3 while the portal asked for v=5."""
        try:
            return str(int((Path(app.static_folder or "static") / filename).stat().st_mtime))
        except OSError:
            return "0"

    def login_required(function: Callable[P, ResponseReturnValue]) -> Callable[P, ResponseReturnValue]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> ResponseReturnValue:
            if not session.get("admin_user"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("admin_login", next=request.path))
            if request.path.startswith("/api/admin/system/") and serial_link().port == "sim://":
                if request.method != "GET" or request.path.endswith("/wifi"):
                    return jsonify({"error": "Host controls are disabled for the simulated inverter"}), 403
            return function(*args, **kwargs)
        return wrapped

    def audit(action: str, detail: str = "") -> None:
        database.audit(now(), session.get("admin_user"), action, detail[:1000])

    def public_sample(sample: dict[str, Any] | None) -> dict[str, Any] | None:
        if sample is None:
            return None
        # This installation has neither a BMS nor a battery-temperature probe.
        # Keep legacy/raw channels in SQLite for diagnostics, but never imply
        # that an unconnected BTS input is a measured battery temperature.
        # `grid_current` is here for the same reason: rows written before
        # 2026-09-16 carry offset 76 under that name, and it was never utility
        # current. The column stays for diagnostics; it must not be served.
        hidden = {"battery_temperature", "bts_temperature", "grid_current"}
        return {key: value for key, value in sample.items() if key not in hidden}

    def live_sample(sample: dict[str, Any] | None) -> dict[str, Any] | None:
        """The latest reading plus what the inverter does not report directly.

        Only the live endpoints pay for this. History rows stay lean -- a
        derived field on every one of 1,500 rows is bulk the charts never read.
        """
        public = public_sample(sample)
        if public is None:
            return None
        return {**public, "utility": utility_model.utility_state(public)}

    # Learning the efficiency curve reads a week of samples, so it is redone
    # at most every 15 minutes per array configuration rather than per page
    # load. Keyed on the geometry so an admin edit takes effect immediately.
    efficiency_cache: dict[str, Any] = {"key": None, "at": 0.0, "value": None}

    def learned_efficiency(lat: float, lon: float, capacity_watts: float, tilt: float | None,
                           azimuth: float | None, dual_slope: bool, now_utc: datetime) -> dict[str, Any] | None:
        key = (lat, lon, capacity_watts, tilt, azimuth, dual_slope)
        if efficiency_cache["key"] == key and time.monotonic() - efficiency_cache["at"] < 900:
            return cast("dict[str, Any] | None", efficiency_cache["value"])
        since = now_utc - timedelta(days=solar_estimate.LEARNING_DAYS)
        samples = database.history(since.isoformat(), max_points=6000)
        learned = solar_estimate.learn_efficiency(
            samples, lat, lon, capacity_watts, tilt, azimuth, dual_slope,
            cloud_by_hour=solar_estimate.fetch_cloud_cover(lat, lon),
        )
        efficiency_cache.update(key=key, at=time.monotonic(), value=learned)
        return learned

    @app.get("/")
    def dashboard() -> ResponseReturnValue:
        return render_template("index.html")

    @app.get("/estimate")
    @app.get("/solar-potential")
    def solar_potential() -> ResponseReturnValue:
        return render_template("estimate.html")

    def status_payload(collector_state: dict[str, Any]) -> dict[str, Any]:
        # Whether the inverter can be written to is a setting the admin
        # portal toggles, so the dashboard must read it rather than assume.
        writes_enabled = inverter_writes_enabled()
        return {
            "sample": live_sample(database.latest_sample()),
            "inverter_config": database.latest_inverter_config(),
            "diagnostic": database.latest_diagnostic(),
            "collector": collector_state,
            "writes_enabled": writes_enabled,
            "read_only": not writes_enabled,
        }

    @app.get("/api/status")
    def status() -> ResponseReturnValue:
        collector_state: dict[str, Any] = (
            collector.state()
            if collector is not None
            else {"running": False, "reason": "collector is disabled"}
        )
        return jsonify(status_payload(collector_state))

    last_manual_refresh = {"at": 0.0}

    @app.post("/api/refresh")
    def refresh() -> ResponseReturnValue:
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
        return jsonify(status_payload(collector.state()))

    def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(request.args.get(name, default))))
        except (TypeError, ValueError):
            return default

    @app.get("/api/history")
    def history() -> ResponseReturnValue:
        hours = _bounded_int("hours", 24, 1, 24 * 30)
        since = datetime.now(UTC) - timedelta(hours=hours)
        max_points = _bounded_int("points", DEFAULT_HISTORY_POINTS, 250, 10000)
        rows = [public_sample(sample) for sample in database.history(since.isoformat(), max_points=max_points)]
        # A caller may ask for only the columns it draws. The full row carries
        # 29 fields; a chart reads five of them, and at 1,400 rows the other
        # 24 are three quarters of a megabyte the client parses and discards.
        wanted = (request.args.get("fields") or "").strip()
        if wanted:
            keep = {name.strip() for name in wanted.split(",") if name.strip()}
            keep.add("captured_at")
            rows = [{key: row[key] for key in keep if key in row} for row in rows if row is not None]
        response = jsonify(rows)
        response.headers["X-History-Points"] = str(len(rows))
        return response

    @app.get("/api/history/daily")
    def history_daily() -> ResponseReturnValue:
        days = _bounded_int("days", 30, 1, 90)
        # Calendar days follow the server timezone, including historical DST.
        local_now = datetime.now().astimezone()
        return jsonify(database.daily_summary(local_now.date().isoformat(), days, utc_offset_minutes=None))

    @app.get("/api/estimate")
    def estimate() -> ResponseReturnValue:
        unconfigured: dict[str, Any] = {
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
        now_utc = datetime.now(UTC)
        window = (now_utc - timedelta(hours=hours), now_utc + FORECAST_PREVIEW)

        # The level of the line is learned from the array's own unconstrained
        # output rather than hand-set. The learned factors already contain
        # whatever the straddling strings cost, so the live line applies the
        # geometry only; the configured ratio (with the full physics) is the
        # fallback until enough readings exist.
        learned = learned_efficiency(
            lat, lon, capacity_watts, tilt_degrees, azimuth_degrees, dual_slope, now_utc
        )
        points, weather_source = solar_estimate.estimate_series(
            lat, lon, capacity_watts, performance_ratio, *window,
            tilt_degrees=tilt_degrees, azimuth_degrees=azimuth_degrees,
            dual_slope=dual_slope, split_strings=split_strings and not learned,
            efficiency=learned["bands"] if learned else None,
        )
        calibration = {
            "source": "learned" if learned else "setting",
            "fallback_ratio": performance_ratio,
            "learning_days": solar_estimate.LEARNING_DAYS,
            **(learned or {"samples": 0, "days": 0, "bands": {}, "peak_factor": None}),
        }

        # What the same panels would collect after the two losses that cost
        # nothing but labour to undo: re-landing each string on one face, and
        # then moving the shaded half onto the sunny slope. The gains are
        # physics ratios over the identical window, applied to the live line
        # so "as built" always equals what the chart shows.
        def window_kwh(series: list[dict[str, Any]]) -> float:
            if len(series) < 2:
                return 0.0
            step_hours = (
                datetime.fromisoformat(series[1]["timestamp"])
                - datetime.fromisoformat(series[0]["timestamp"])
            ).total_seconds() / 3600
            return sum(float(point["estimated_watts"]) for point in series) * step_hours / 1000

        as_built = window_kwh(points)
        scenarios = {"as_built_kwh": round(as_built, 2)}
        if dual_slope and tilt_degrees is not None and azimuth_degrees is not None:
            def physics_kwh(*, dual_slope: bool, split_strings: bool) -> float:
                series, _ = solar_estimate.estimate_series(
                    lat, lon, capacity_watts, performance_ratio, *window,
                    tilt_degrees=tilt_degrees, azimuth_degrees=azimuth_degrees,
                    dual_slope=dual_slope, split_strings=split_strings,
                )
                return window_kwh(series)
            baseline = physics_kwh(dual_slope=True, split_strings=split_strings) or 1.0
            per_face = physics_kwh(dual_slope=True, split_strings=False) / baseline
            one_plane = physics_kwh(dual_slope=False, split_strings=False) / baseline
            scenarios.update({
                "strings_per_face_kwh": round(as_built * per_face, 2),
                "all_one_slope_kwh": round(as_built * one_plane, 2),
            })
        return jsonify({
            "configured": True,
            "location": {"latitude": lat, "longitude": lon},
            "capacity_watts": capacity_watts,
            "performance_ratio": learned["peak_factor"] if learned else performance_ratio,
            "calibration": calibration,
            # Hardware facts come from the site profile and are null when it
            # does not name them; the geometry is the admin's live setting.
            "array": {
                "description": profile.array_description,
                "module_count": profile.module_count,
                "module_watts": profile.module_watts,
                "wiring": profile.wiring,
                "vmp_volts": profile.vmp_volts,
                "voc_volts": profile.voc_volts,
                "imp_amps": profile.imp_amps,
                "isc_amps": profile.isc_amps,
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
                "inverter": profile.inverter_model,
                "battery_bank": profile.battery_description,
                "battery_nominal_volts": profile.battery_nominal_volts,
                "battery_capacity_ah": battery_capacity_ah(settings),
                "configured_charge_limit_amps": profile.charge_limit_amps,
            },
            "weather_source": weather_source,
            "scenarios": scenarios,
            "points": points,
        })

    # State of charge by counting charge, re-anchored on full and rest, with
    # per-day health gauges. Two weeks of readings go into it, so it is
    # recomputed at most once a minute rather than per request.
    battery_cache: dict[str, Any] = {"at": 0.0, "value": None}

    @app.get("/api/battery")
    def battery_state() -> ResponseReturnValue:
        if battery_cache["value"] and time.monotonic() - battery_cache["at"] < 60:
            return jsonify(battery_cache["value"])
        settings = database.get_settings()
        nominal = battery_capacity_ah(settings)
        config = database.latest_inverter_config() or {}
        try:
            bulk = float((config.get("settings") or {}).get("bulk_charge_voltage_v") or 28.2)
        except (TypeError, ValueError):
            bulk = 28.2
        since = datetime.now(UTC) - timedelta(days=14)
        samples = database.history(since.isoformat(), max_points=8000)
        try:
            cutoff = float((config.get("settings") or {}).get("low_battery_cutoff_v") or 21.0)
        except (TypeError, ValueError):
            cutoff = 21.0
        latest = database.latest_sample() or {}
        # With no utility wired in, the transfer-to-grid threshold cannot act
        # and the low-voltage cutoff is a hard stop with nothing to fall back
        # to. That is what made 2026-09-05 a shutdown rather than a transfer.
        grid_available = utility_model.utility_state(latest)["present"]
        # With no nameplate, count_charge returns its "unavailable" state:
        # the payload keeps its shape and the capacity is honestly null.
        state = battery_model.count_charge(samples, nominal, bulk_setpoint_v=bulk)
        payload = {
            "state": state,
            "runtime": battery_model.runtime_to_cutoff(samples, state, cutoff, grid_available),
            "cutoff_volts": cutoff,
            "grid_available": grid_available,
            "days": battery_model.daily_health(samples, bulk, utc_offset_minutes=None)[-14:],
            "nominal_ah": nominal,
            "bulk_setpoint_v": bulk,
            "computed_at": now(),
        }
        battery_cache.update(at=time.monotonic(), value=payload)
        return jsonify(payload)

    @app.get("/api/history/faults")
    def history_faults() -> ResponseReturnValue:
        days = _bounded_int("days", 30, 1, 90)
        since = datetime.now(UTC) - timedelta(days=days)
        return jsonify(database.fault_history(since.isoformat()))

    @app.get("/api/system")
    def system_public() -> ResponseReturnValue:
        return jsonify(system_info.public_snapshot(include_network=expose_network_info))

    @app.get("/health")
    def health() -> ResponseReturnValue:
        collector_state: dict[str, Any] = (
            collector.state()
            if collector is not None
            else {"running": False, "reason": "collector is disabled"}
        )
        # A web-only instance is healthy by itself. One with a collector is
        # healthy only while the serial link answers: the last successful
        # exchange must be younger than three poll intervals (150 s at
        # least), so a poller that is alive but failing every cycle is down.
        stale_after = max(3 * int(collector_state.get("interval_seconds") or 0), 150)
        age = collector_state.get("last_success_age_seconds")
        running = bool(collector_state.get("running"))
        ok = collector is None or (running and age is not None and age < stale_after)
        payload = {
            "ok": ok,
            "read_only": database.get_settings().get("writes_enabled") != "true",
            "collector_enabled": collector is not None,
            "collector_running": running,
            "stale_after_seconds": stale_after if collector is not None else None,
            "collector": collector_state,
        }
        return jsonify(payload), (200 if ok else 503)

    @app.route("/admin/setup", methods=["GET", "POST"])
    def admin_setup() -> ResponseReturnValue:
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

    # Password guesses per account. The PIN locks for good after three
    # failures because the password stays as the way back in; the password
    # has no way back, so it locks for a while instead: long enough to make
    # guessing hopeless, short enough that the only admin is never shut out.
    # Past the limit every further wrong password locks it again, so after
    # the first lock a guesser gets one try per lock; a correct password
    # clears the count. Held in memory, so a restart clears it -- and restarting the service
    # already takes access an attacker would not be guessing for.
    password_failures: dict[str, tuple[int, float]] = {}

    def password_locked_for(username: str) -> float:
        failures, locked_until = password_failures.get(username, (0, 0.0))
        return max(0.0, locked_until - time.monotonic()) if failures >= PASSWORD_ATTEMPTS else 0.0

    def record_password_failure(username: str) -> None:
        failures, _ = password_failures.get(username, (0, 0.0))
        failures += 1
        locked_until = time.monotonic() + PASSWORD_LOCKOUT_SECONDS if failures >= PASSWORD_ATTEMPTS else 0.0
        password_failures[username] = (failures, locked_until)
        if failures == PASSWORD_ATTEMPTS:
            database.audit(now(), username, "auth.locked",
                           f"{failures} wrong passwords; locked for {PASSWORD_LOCKOUT_SECONDS // 60} minutes")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login() -> ResponseReturnValue:
        if not database.admin_count():
            return render_template("admin_unconfigured.html")
        error = None
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = database.admin_by_username(username)
            wait = password_locked_for(username) if user else 0.0
            if wait:
                # Refused before the hash is checked, so a locked account
                # cannot be probed for its password at all.
                minutes = max(1, round(wait / 60))
                error = f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
            elif user and check_password_hash(user["password_hash"], password):
                password_failures.pop(username, None)
                session.clear()
                session["admin_user"] = username
                session["auth_method"] = "password"
                session.permanent = True
                database.record_login(username, now())
                database.reset_pin_failures(username)
                audit("auth.login")
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/admin") else url_for("admin_home"))
            else:
                database.audit(now(), username or None, "auth.failed", request.remote_addr or "")
                if user:
                    record_password_failure(username)
                error = "Incorrect username or password."
        pin_admin = database.pin_admin()
        return render_template(
            "admin_login.html", error=error,
            pin_available=bool(pin_admin and not pin_admin["pin_locked"]),
            pin_locked=bool(pin_admin and pin_admin["pin_locked"]),
            pin_error=request.args.get("pin_error"),
        )

    @app.post("/admin/pin-login")
    def admin_pin_login() -> ResponseReturnValue:
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
    def admin_logout() -> ResponseReturnValue:
        audit("auth.logout")
        session.clear()
        return redirect(url_for("admin_login"))

    @app.get("/admin")
    @login_required
    def admin_home() -> ResponseReturnValue:
        return render_template("admin.html", username=session["admin_user"])

    def serial_link() -> SerialConfig:
        """The link the collector actually runs on, from the config file."""
        return collector.config if collector is not None else serial_config

    @app.get("/api/admin/overview")
    @login_required
    def admin_overview() -> ResponseReturnValue:
        disk = shutil.disk_usage(database.path.parent)
        link = serial_link()
        return jsonify({
            "ports": discover_usb_serial_ports(),
            "serial_link": {
                "configured_port": link.port,
                "port_in_use": collector.last_port if collector is not None else None,
                "baudrate": link.baudrate,
                "timeout_seconds": link.timeout_seconds,
                "protocol": create_protocol(link.protocol).name,
            },
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
    def system_reboot() -> ResponseReturnValue:
        ok, message = privileged.reboot()
        audit("system.reboot", "requested" if ok else message)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.post("/api/admin/system/restart-service")
    @login_required
    def system_restart_service() -> ResponseReturnValue:
        ok, message = privileged.restart_service()
        audit("system.restart_service", "requested" if ok else message)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.route("/api/admin/system/eco", methods=["GET", "POST"])
    @login_required
    def system_eco() -> ResponseReturnValue:
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
    def system_wifi_scan() -> ResponseReturnValue:
        ok, message = privileged.wifi_scan()
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"networks": privileged.parse_wifi_scan(message)})

    @app.post("/api/admin/system/wifi")
    @login_required
    def system_wifi_connect() -> ResponseReturnValue:
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
    def system_update_check() -> ResponseReturnValue:
        result = system_info.check_updates(Path(__file__).resolve().parent.parent)
        audit("system.update_check")
        return jsonify(result)

    @app.get("/api/admin/frames")
    @login_required
    def admin_frames() -> ResponseReturnValue:
        limit = _bounded_int("limit", 100, 1, 500)
        return jsonify(database.raw_frames(limit))

    @app.post("/api/admin/data/cleanup")
    @login_required
    def admin_data_cleanup() -> ResponseReturnValue:
        settings = database.get_settings()
        result = database.cleanup_retention(
            int(settings.get("raw_success_days", "7")),
            int(settings.get("raw_diagnostic_days", "90")),
        )
        audit("data.cleanup", json.dumps(result, sort_keys=True))
        return jsonify({"ok": True, **result, "counts": database.storage_counts()})

    @app.get("/api/admin/audit")
    @login_required
    def admin_audit() -> ResponseReturnValue:
        return jsonify(database.recent_audit(100))

    @app.route("/api/admin/security", methods=["GET", "POST"])
    @login_required
    def admin_security() -> ResponseReturnValue:
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
        return jsonify({"pin_enabled": bool(user and user["pin_hash"]), "pin_locked": False,
                        "pin_failures": 0, "auth_method": session.get("auth_method")})

    @app.route("/api/admin/settings", methods=["GET", "POST"])
    @login_required
    def admin_settings() -> ResponseReturnValue:
        if request.method == "GET":
            return jsonify(database.get_settings())
        payload = request.get_json(silent=True) or {}
        allowed = {
            "poll_seconds", "raw_success_days", "raw_diagnostic_days",
            "estimator_latitude", "estimator_longitude",
            "estimator_capacity_watts", "estimator_performance_ratio",
            "estimator_tilt_degrees", "estimator_azimuth_degrees",
            "estimator_dual_slope", "estimator_split_strings",
            "battery_capacity_ah",
        }
        values = {key: str(value).strip() for key, value in payload.items() if key in allowed}
        try:
            if "poll_seconds" in values and not 10 <= int(values["poll_seconds"]) <= 3600:
                raise ValueError("Polling must be between 10 and 3600 seconds")
            # 0 days is "keep forever", longer than any window.
            if "raw_success_days" in values and not 0 <= int(values["raw_success_days"]) <= 30:
                raise ValueError("Routine frame retention must be 1 to 30 days, or 0 to keep forever")
            if "raw_diagnostic_days" in values and not (
                int(values["raw_diagnostic_days"]) == 0 or 7 <= int(values["raw_diagnostic_days"]) <= 365
            ):
                raise ValueError("Diagnostic retention must be 7 to 365 days, or 0 to keep forever")
            merged = {**database.get_settings(), **values}

            def window(key: str, default: str) -> float:
                days = int(merged.get(key, default))
                return float("inf") if days == 0 else days
            if window("raw_diagnostic_days", "90") < window("raw_success_days", "7"):
                raise ValueError("Diagnostic retention cannot be shorter than routine retention")
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
            if values.get("battery_capacity_ah") and not 10 <= float(values["battery_capacity_ah"]) <= 5000:
                raise ValueError("Battery capacity must be between 10 and 5,000 Ah")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        database.set_settings(values, now())
        if collector is not None and "poll_seconds" in values:
            collector.set_poll_seconds(int(values["poll_seconds"]))
        audit("settings.updated", json.dumps(values, sort_keys=True))
        return jsonify(database.get_settings())

    @app.post("/api/admin/serial/<mode>")
    @login_required
    def admin_serial(mode: str) -> ResponseReturnValue:
        if mode not in {"listen", "probe"}:
            abort(404)
        link = serial_link()
        port = link.port
        if port == "auto":
            ports = discover_usb_serial_ports()
            port = ports[0] if ports else ""
        if not port:
            return jsonify({"error": "No USB serial adapter detected"}), 400
        protocol = collector.protocol if collector is not None else create_protocol(link.protocol)
        # The collector's lock, so a diagnostic never opens the port in the
        # middle of a poll: two handles on one line interleave their bytes,
        # and both sides read the other's reply as garbage.
        with collector.lock if collector is not None else nullcontext():
            if mode == "listen":
                try:
                    with serial.serial_for_url(port, link.baudrate, timeout=link.timeout_seconds) as connection:
                        response = connection.read(4096)
                except (serial.SerialException, OSError, ValueError) as exc:
                    return jsonify({"error": str(exc)}), 400
                capture = Exchange(
                    now(), port, link.baudrate, "listen_only", b"", response, int(link.timeout_seconds * 1000)
                )
                record = capture.record(protocol.name, "Passive listen-only capture", decoded=False)
            else:
                result = exchange(port, link.baudrate, link.timeout_seconds, protocol, protocol.queries[0])
                decoded = protocol.decode(result.response)
                record = result.record(protocol.name, decoded.note, decoded.valid)
        database.save_raw_frame(record)
        audit(f"serial.{mode}", f"{port} @ {link.baudrate}")
        return jsonify(record), (400 if record.get("error") else 200)

    # --- Inverter settings ---------------------------------------------------
    # The one place the monitor is not read-only. Gated by an admin-controlled
    # switch that defaults to off, and every write goes through the protocol's
    # own gate (only the inverter's last reply, only whitelisted fields, only
    # in range) before it reaches the port. Nothing is reported as applied on
    # the strength of having been sent: the collector reads the block back
    # and the response says whether the inverter now holds each value.

    def inverter_writes_enabled() -> bool:
        return database.get_settings().get("writes_enabled") == "true"

    def writable_fields() -> dict[str, Any]:
        protocol = collector.protocol if collector is not None else create_protocol(serial_config.protocol)
        return {
            name: {
                "kind": field.kind, "scale": field.scale, "low": field.low,
                "high": field.high, "choices": list(field.choices),
            }
            for name, field in getattr(protocol, "writable_fields", {}).items()
        }

    @app.get("/api/admin/inverter")
    @login_required
    def admin_inverter() -> ResponseReturnValue:
        settings = database.get_settings()
        database.expire_change_requests(
            (datetime.now(UTC) - timedelta(hours=propose.PROPOSAL_TTL_HOURS)).isoformat()
        )
        return jsonify({
            "writes_enabled": inverter_writes_enabled(),
            "collector_available": collector is not None,
            "current": database.latest_inverter_config(),
            "fields": writable_fields(),
            "snapshots": database.inverter_config_changes(30),
            "requests": database.change_requests(25),
            "auto_apply": settings.get(change_policy.ENABLED_SETTING) == "true",
            "auto_apply_policy": settings.get(change_policy.POLICY_SETTING) or "",
        })

    @app.post("/api/admin/inverter/requests/<int:request_id>/<decision>")
    @login_required
    def admin_decide_request(request_id: int, decision: str) -> ResponseReturnValue:
        if decision not in {"approve", "reject"}:
            abort(404)
        database.expire_change_requests(
            (datetime.now(UTC) - timedelta(hours=propose.PROPOSAL_TTL_HOURS)).isoformat()
        )
        request_row = database.change_request(request_id)
        if request_row is None:
            return jsonify({"error": "No such change request"}), 404
        if request_row["status"] != "pending":
            return jsonify({"error": f"That request is already {request_row['status']}"}), 409
        if decision == "approve" and not inverter_writes_enabled():
            return jsonify({"error": "Inverter writes are switched off"}), 403
        status = "approved" if decision == "approve" else "rejected"
        if not database.decide_change_request(request_id, status, session["admin_user"], now()):
            return jsonify({"error": "That request was already decided"}), 409
        audit(f"inverter.change.{status}", f"request {request_id}: {request_row['changes']}")
        # Approved requests are written by the collector, which owns the port.
        outcomes = collector.apply_approved_changes() if (collector and status == "approved") else []
        return jsonify({
            "status": status,
            "applied": next((o for o in outcomes if o["id"] == request_id), None),
            "request": database.change_request(request_id),
        })

    @app.post("/api/admin/inverter/auto-apply")
    @login_required
    def admin_auto_apply() -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        values = {change_policy.ENABLED_SETTING: "true" if payload.get("enabled") else "false"}
        if "policy" in payload:
            if not isinstance(payload["policy"], str):
                return jsonify({"error": "policy must be a JSON string"}), 400
            raw = payload["policy"].strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError as exc:
                    return jsonify({"error": f"The policy is not valid JSON: {exc}"}), 400
                if not isinstance(parsed, dict):
                    return jsonify({"error": "The policy must be an object of field rules"}), 400
                unknown = sorted(set(parsed) - set(writable_fields()))
                if unknown:
                    return jsonify({"error": f"Not writable settings: {', '.join(unknown)}"}), 400
                try:
                    change_policy.validate_policy(parsed, writable_fields())
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400
                raw = json.dumps(parsed, sort_keys=True, indent=2)
            values[change_policy.POLICY_SETTING] = raw
        with collector.lock if collector is not None else nullcontext():
            database.set_settings(values, now())
        audit("inverter.auto_apply", json.dumps(values, sort_keys=True))
        return jsonify({
            "auto_apply": values[change_policy.ENABLED_SETTING] == "true",
            "auto_apply_policy": database.get_settings().get(change_policy.POLICY_SETTING, ""),
        })

    @app.post("/api/admin/inverter/writes")
    @login_required
    def admin_inverter_writes() -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        enabled = payload["enabled"]
        with collector.lock if collector is not None else nullcontext():
            database.set_settings({"writes_enabled": "true" if enabled else "false"}, now())
        audit("inverter.writes", "enabled" if enabled else "disabled")
        return jsonify({"writes_enabled": enabled})

    def perform_inverter_write(changes: dict[str, Any], origin: str) -> ResponseReturnValue:
        if collector is None:
            return jsonify({"error": "The collector is disabled on this instance"}), 503
        if not inverter_writes_enabled():
            return jsonify({"error": "Inverter writes are switched off"}), 403
        fields = writable_fields()
        unknown = sorted(set(changes) - set(fields))
        if unknown:
            return jsonify({"error": f"Not writable: {', '.join(unknown)}"}), 400
        if not changes:
            return jsonify({"error": "No settings to change"}), 400
        try:
            result = collector.apply_config(changes)
        except (ValueError, PermissionError) as exc:
            audit("inverter.write_failed", json.dumps({"origin": origin, "changes": changes, "error": str(exc)}, sort_keys=True))
            return jsonify({"error": str(exc)}), 400
        audit("inverter.write", json.dumps({
            "origin": origin, "changes": changes,
            "verified": result["verified"], "mismatches": result["mismatches"],
        }, sort_keys=True, default=str))
        return jsonify(result)

    @app.post("/api/admin/inverter/settings")
    @login_required
    def admin_inverter_write() -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            return jsonify({"error": "Expected a 'changes' object"}), 400
        return perform_inverter_write(changes, "form")

    @app.post("/api/admin/inverter/restore")
    @login_required
    def admin_inverter_restore() -> ResponseReturnValue:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        try:
            snapshot = database.inverter_config_snapshot(int(payload.get("snapshot_id") or 0))
        except (TypeError, ValueError):
            snapshot = None
        if snapshot is None:
            return jsonify({"error": "No such configuration snapshot"}), 404
        fields = writable_fields()
        changes = {key: value for key, value in snapshot["settings"].items() if key in fields}
        return perform_inverter_write(changes, f"restore snapshot {snapshot['id']} ({snapshot['captured_at']})")

    @app.get("/api/admin/export/frames.jsonl")
    @login_required
    def export_frames() -> ResponseReturnValue:
        rows = reversed(database.raw_frames(500))
        body = "".join(json.dumps(row, default=str) + "\n" for row in rows)
        audit("data.export", "frames.jsonl")
        return Response(body, mimetype="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=solar-frames.jsonl"})

    @app.get("/api/admin/export/samples.csv")
    @login_required
    def export_samples() -> ResponseReturnValue:
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
    return datetime.now(UTC).isoformat()
