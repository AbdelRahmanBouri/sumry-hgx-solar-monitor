from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solar_monitor.db import Database
from solar_monitor.web import create_app


def hours_ago(hours: float) -> str:
    # The history endpoints window on "now", so fixtures have to be stamped
    # relative to it. Hardcoded dates quietly stop being inside the window.
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")
        self.client = create_app(self.db).test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_health_is_read_only(self):
        response = self.client.get("/health")
        payload = response.json
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["collector_enabled"])
        self.assertFalse(payload["collector_running"])
        self.assertIsNone(payload["stale_after_seconds"])

    def test_health_read_only_follows_the_writes_switch(self):
        self.db.set_settings({"writes_enabled": "true"}, "t")
        self.assertFalse(self.client.get("/health").json["read_only"])
        self.db.set_settings({"writes_enabled": "false"}, "t")
        self.assertTrue(self.client.get("/health").json["read_only"])

    def _health_with(self, running, age, interval=30):
        class FakeCollector:
            def state(self):
                return {"running": running, "interval_seconds": interval,
                        "last_success_age_seconds": age}

            def set_poll_seconds(self, seconds):
                pass

        response = create_app(self.db, collector=FakeCollector()).test_client().get("/health")
        return response.status_code, response.json

    def test_health_reflects_a_working_serial_link(self):
        status, payload = self._health_with(True, 12.0)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["collector_running"])
        self.assertEqual(payload["stale_after_seconds"], 150)

        # Alive but failing every poll: the last success ages past the limit.
        status, payload = self._health_with(True, 150.0)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["collector_running"])
        self.assertEqual(payload["collector"]["last_success_age_seconds"], 150.0)

        # Never answered at all.
        status, payload = self._health_with(True, None)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

        # Thread dead, however fresh the last success.
        status, payload = self._health_with(False, 1.0)
        self.assertEqual(status, 503)
        self.assertFalse(payload["collector_running"])

    def test_health_stale_limit_is_three_poll_intervals(self):
        status, payload = self._health_with(True, 300.0, interval=120)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale_after_seconds"], 360)
        status, _ = self._health_with(True, 360.0, interval=120)
        self.assertEqual(status, 503)

    def test_health_is_503_for_a_real_collector_that_is_not_running(self):
        from solar_monitor.collector import Collector
        from solar_monitor.config import SerialConfig
        collector = Collector(SerialConfig(), self.db)
        response = create_app(self.db, collector=collector).test_client().get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.json["collector"]["last_success_age_seconds"])

    def test_empty_status(self):
        payload = self.client.get("/api/status").json
        self.assertIsNone(payload["sample"])
        self.assertTrue(payload["read_only"])

    def test_status_derives_utility_use_from_the_transfer_relay(self):
        # A night-time bypass reading: mains present, the inverter's own
        # output collapsed, and the load still served.
        self.db.save_sample("2026-09-16T04:00:00+00:00", {
            "grid_voltage": 121.6, "grid_frequency": 59.97,
            "output_voltage": 121.2, "output_current": 0.08,
            "ac_load_watts": 370.0, "pv_watts": 0.0,
            "battery_voltage": 25.72, "battery_current": 0.10,
        })
        sample = self.client.get("/api/status").json["sample"]
        self.assertEqual(sample["utility"]["state"], "carrying")
        self.assertTrue(sample["utility"]["carrying_load"])

    def test_legacy_grid_current_is_never_served_again(self):
        # Rows written before 2026-09-16 carry offset 76 under this name. The
        # column keeps them for diagnostics; no endpoint may hand them out.
        self.db.save_sample("2026-09-16T04:00:00+00:00", {
            "grid_voltage": 121.6, "ac_load_watts": 370.0,
        })
        with self.db.connect() as connection:
            connection.execute("UPDATE samples SET grid_current = 0.9")
            self.assertEqual(
                connection.execute("SELECT grid_current FROM samples").fetchone()[0], 0.9
            )

        self.assertNotIn("grid_current", self.client.get("/api/status").json["sample"])
        rows = self.client.get("/api/history?hours=9999").json
        self.assertEqual(len(rows), 1)
        self.assertNotIn("grid_current", rows[0])
        # The derived verdict belongs to the live endpoints; adding it to
        # every history row is bulk the charts never read.
        self.assertNotIn("utility", rows[0])

    def test_a_decoder_can_no_longer_write_grid_current(self):
        self.db.save_sample("2026-09-16T05:00:00+00:00", {
            "grid_voltage": 121.6, "grid_current": 0.9,
        })
        with self.db.connect() as connection:
            stored = connection.execute("SELECT grid_current FROM samples").fetchone()[0]
        self.assertIsNone(stored)

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Writes disabled", response.data)

    def test_solar_potential_page_is_separate_from_live_chart(self):
        response = self.client.get("/estimate")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Solar potential", response.data)
        self.assertIn(b'id="opportunity-chart"', response.data)
        self.assertIn(b'id="available-solar"', response.data)
        self.assertIn(b"/static/estimate.js", response.data)

        alias = self.client.get("/solar-potential")
        self.assertEqual(alias.status_code, 200)

    def test_retired_dashboards_are_gone(self):
        for path in ("/lab", "/v2"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_dashboard_loads_its_own_assets(self):
        response = self.client.get("/")
        self.assertIn(b"/static/style.css", response.data)
        self.assertIn(b"/static/dashboard.css", response.data)
        self.assertIn(b"/static/app.js", response.data)
        self.assertIn(b"/static/metric-picker.js", response.data)

    def test_dashboard_renders_chart_tooling(self):
        for path in ("/",):
            response = self.client.get(path)
            self.assertIn(b'id="chart-inspect"', response.data)
            self.assertIn(b'id="chart-pan"', response.data)
            self.assertIn(b'id="chart-export"', response.data)
            self.assertIn(b'id="chart-table-body"', response.data)
            self.assertIn(b'id="chart-y-scale"', response.data)
            self.assertIn(b'id="battery-soc"', response.data)
            self.assertNotIn(b'Energy source', response.data)
            self.assertIn(b'user-scalable=no', response.data)

    def test_unconnected_battery_temperature_channels_are_not_public(self):
        self.db.save_sample(
            "2026-09-03T00:00:00+00:00",
            {"battery_voltage": 24.8, "battery_temperature": 25, "bts_temperature": 25},
        )
        sample = self.client.get("/api/status").json["sample"]
        self.assertEqual(sample["battery_voltage"], 24.8)
        self.assertNotIn("battery_temperature", sample)
        self.assertNotIn("bts_temperature", sample)

    def test_admin_requires_setup_and_login(self):
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        response = self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 200)

    def test_setup_rejects_bad_token(self):
        self.assertEqual(self.client.get("/admin/setup?token=wrong").status_code, 404)

    def test_history_accepts_hours_param_and_bounds_it(self):
        # Both samples sit outside a one-hour window and inside the 30-day
        # ceiling that hours=9999 is clamped down to.
        self.db.save_sample(hours_ago(48), {"pv_watts": 100})
        self.db.save_sample(hours_ago(2), {"pv_watts": 200})
        recent = self.client.get("/api/history?hours=1").json
        self.assertEqual(len(recent), 0)
        longer = self.client.get("/api/history?hours=9999").json
        self.assertEqual(len(longer), 2)

    def test_history_daily_and_faults_endpoints(self):
        # Midday yesterday: comfortably inside the seven-day window, and far
        # enough from midnight that both samples share one UTC calendar day,
        # which is what daily_summary groups on.
        midday = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        self.db.save_sample(midday.isoformat(), {"pv_watts": 100, "faults": "Overload"})
        self.db.save_sample(
            (midday + timedelta(hours=1)).isoformat(), {"pv_watts": 300, "faults": "None"}
        )
        daily = self.client.get("/api/history/daily?days=7").json
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["avg_pv_watts"], 200)
        faults = self.client.get("/api/history/faults?days=7").json
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["faults"], "Overload")

    def test_refresh_requires_a_collector_and_is_rate_limited(self):
        self.assertEqual(self.client.post("/api/refresh").status_code, 503)

        class FakeCollector:
            def __init__(self):
                self.poll_seconds = None

            def collect_once(self):
                return True

            def state(self):
                return {"running": True}

            def set_poll_seconds(self, seconds):
                self.poll_seconds = seconds

        client = create_app(self.db, collector=FakeCollector()).test_client()
        first = client.post("/api/refresh")
        self.assertEqual(first.status_code, 200)
        second = client.post("/api/refresh")
        self.assertEqual(second.status_code, 429)
        self.assertIn("retry_after_seconds", second.json)

    def test_polling_setting_is_applied_to_running_collector(self):
        class FakeCollector:
            def __init__(self):
                self.poll_seconds = None

            def state(self):
                return {"running": True}

            def set_poll_seconds(self, seconds):
                self.poll_seconds = seconds

        collector = FakeCollector()
        client = create_app(self.db, collector=collector).test_client()
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        response = client.post("/api/admin/settings", json={"poll_seconds": "10"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(collector.poll_seconds, 10)

    def _admin_client(self, collector=None):
        client = create_app(self.db, collector=collector).test_client()
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        return client

    def test_inverter_writes_are_off_by_default_and_gated(self):
        class FakeCollector:
            from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol
            protocol = Sumry8851Protocol()
            calls = []

            def state(self):
                return {"running": True}

            def set_poll_seconds(self, seconds):
                pass

            def apply_config(self, changes, settle_seconds=0.6):
                self.calls.append(changes)
                return {"requested": changes, "before": {}, "after": dict(changes),
                        "verified": True, "mismatches": {}, "written_at": "t", "write_reply_hex": ""}

        collector = FakeCollector()
        client = self._admin_client(collector)
        overview = client.get("/api/admin/inverter").json
        self.assertFalse(overview["writes_enabled"])
        self.assertIn("bulk_charge_voltage_v", overview["fields"])
        self.assertEqual(overview["fields"]["bulk_charge_voltage_v"]["high"], 29.2)

        refused = client.post("/api/admin/inverter/settings", json={"changes": {"buzzer": False}})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(collector.calls, [])

        client.post("/api/admin/inverter/writes", json={"enabled": True})
        self.assertTrue(client.get("/api/admin/inverter").json["writes_enabled"])
        bad = client.post("/api/admin/inverter/settings", json={"changes": {"output_voltage_v": 110}})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(collector.calls, [])

        ok = client.post("/api/admin/inverter/settings", json={"changes": {"bulk_charge_voltage_v": 29.2}})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json["verified"])
        self.assertEqual(collector.calls, [{"bulk_charge_voltage_v": 29.2}])
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("inverter.writes", actions)
        self.assertIn("inverter.write", actions)

        missing = client.post("/api/admin/inverter/restore", json={"snapshot_id": 999})
        self.assertEqual(missing.status_code, 404)

    def test_inverter_write_needs_a_collector(self):
        client = self._admin_client()
        client.post("/api/admin/inverter/writes", json={"enabled": True})
        response = client.post("/api/admin/inverter/settings", json={"changes": {"buzzer": False}})
        self.assertEqual(response.status_code, 503)

    def test_public_system_endpoint_is_available_without_login(self):
        payload = self.client.get("/api/system").json
        self.assertIn("uptime_seconds", payload)
        self.assertIn("network", payload)

    def test_admin_system_actions_require_login(self):
        for path in (
            "/api/admin/system/reboot", "/api/admin/system/restart-service",
            "/api/admin/system/eco", "/api/admin/system/wifi", "/api/admin/system/update-check",
        ):
            self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/system/wifi").status_code, 401)

    def test_admin_privileged_actions_fail_gracefully_without_sudoers_setup(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        with patch("solar_monitor.web.privileged.reboot", return_value=(False, "not configured")):
            response = self.client.post("/api/admin/system/reboot")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])

    def test_site_location_defaults_are_seeded(self):
        settings = self.db.get_settings()
        self.assertTrue(float(settings["estimator_latitude"]))
        self.assertTrue(float(settings["estimator_longitude"]))
        self.assertGreater(float(settings["estimator_capacity_watts"]), 0)

    def test_blank_estimator_settings_are_backfilled_on_start(self):
        # An earlier release seeded these as empty strings.
        self.db.set_settings({"estimator_latitude": "", "estimator_capacity_watts": ""}, "now")
        create_app(self.db)
        settings = self.db.get_settings()
        self.assertTrue(float(settings["estimator_latitude"]))
        self.assertGreater(float(settings["estimator_capacity_watts"]), 0)

    def test_a_cleared_capacity_switches_the_estimator_off(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        self.client.post("/api/admin/settings", json={"estimator_capacity_watts": "0"})
        payload = self.client.get("/api/estimate").json
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["points"], [])

    def test_estimate_returns_points_once_configured(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        response = self.client.post("/api/admin/settings", json={
            "estimator_latitude": "34.0522",
            "estimator_longitude": "-118.2437",
            "estimator_capacity_watts": "600",
            "estimator_performance_ratio": "0.8",
        })
        self.assertEqual(response.status_code, 200)
        with patch("solar_monitor.solar_estimate.fetch_cloud_cover", return_value=None):
            payload = self.client.get("/api/estimate?hours=6").json
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["weather_source"], "clear-sky-only")
        self.assertTrue(payload["points"])
        self.assertIn("estimated_watts", payload["points"][0])
        self.assertEqual(payload["array"]["wiring"], "4S2P")
        self.assertEqual(payload["array"]["module_count"], 8)
        self.assertEqual(payload["system"]["configured_charge_limit_amps"], 80)
        # An empty database has nothing to learn from, so the configured
        # ratio applies and the response says so.
        self.assertEqual(payload["calibration"]["source"], "setting")
        self.assertEqual(payload["calibration"]["fallback_ratio"], 0.8)
        self.assertEqual(payload["performance_ratio"], 0.8)

    def test_estimate_previews_the_hours_ahead_for_any_lookback(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        self.client.post("/api/admin/settings", json={
            "estimator_latitude": "34.0522",
            "estimator_longitude": "-118.2437",
            "estimator_capacity_watts": "600",
        })
        for hours in (1, 24, 48):
            with patch("solar_monitor.solar_estimate.fetch_cloud_cover", return_value=None):
                points = self.client.get(f"/api/estimate?hours={hours}").json["points"]
            self.assertTrue(
                any(point["forecast"] for point in points),
                f"hours={hours} returned no forecast points",
            )
            self.assertTrue(any(not point["forecast"] for point in points))

    def test_estimate_settings_are_validated(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        response = self.client.post(
            "/api/admin/settings", json={"estimator_latitude": "200"}
        )
        self.assertEqual(response.status_code, 400)

    def test_pin_locks_after_three_failures_and_password_resets_it(self):
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        self.client.post(
            f"/admin/setup?token={token}",
            data={"token": token, "username": "admin", "password": "correct-horse-1",
                  "confirmation": "correct-horse-1"},
        )
        response = self.client.post(
            "/api/admin/security", json={"action": "set", "pin": "246810"}
        )
        self.assertEqual(response.status_code, 200)
        self.client.post("/admin/logout")
        for _ in range(3):
            self.client.post("/admin/pin-login", data={"pin": "0000"})
        self.assertTrue(self.db.admin_by_username("admin")["pin_locked"])
        self.assertEqual(
            self.client.post("/admin/pin-login", data={"pin": "246810"}).status_code, 302
        )
        self.client.post(
            "/admin/login", data={"username": "admin", "password": "correct-horse-1"}
        )
        self.assertFalse(self.db.admin_by_username("admin")["pin_locked"])
        self.client.post("/admin/logout")
        response = self.client.post("/admin/pin-login", data={"pin": "246810"})
        self.assertEqual(response.location, "/admin")


if __name__ == "__main__":
    unittest.main()
