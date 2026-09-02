import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(self.client.get("/health").json, {"ok": True, "read_only": True})

    def test_empty_status(self):
        payload = self.client.get("/api/status").json
        self.assertIsNone(payload["sample"])
        self.assertTrue(payload["read_only"])

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Writes disabled", response.data)

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
