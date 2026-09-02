import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
