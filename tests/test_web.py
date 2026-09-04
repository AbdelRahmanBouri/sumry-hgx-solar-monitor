import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solar_monitor.db import Database
from solar_monitor.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")
        self.client = create_app(self.db).test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_health_is_read_only(self):
        payload = self.client.get("/health").json
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["collector_enabled"])
        self.assertFalse(payload["collector_running"])

    def test_empty_status(self):
        payload = self.client.get("/api/status").json
        self.assertIsNone(payload["sample"])
        self.assertTrue(payload["read_only"])

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
        self.db.save_sample("2026-09-01T00:00:00+00:00", {"pv_watts": 100})
        self.db.save_sample("2026-09-03T00:00:00+00:00", {"pv_watts": 200})
        recent = self.client.get("/api/history?hours=1").json
        self.assertEqual(len(recent), 0)
        longer = self.client.get("/api/history?hours=9999").json
        self.assertEqual(len(longer), 2)

    def test_history_daily_and_faults_endpoints(self):
        self.db.save_sample("2026-09-02T12:00:00+00:00", {"pv_watts": 100, "faults": "Overload"})
        self.db.save_sample("2026-09-02T13:00:00+00:00", {"pv_watts": 300, "faults": "None"})
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
