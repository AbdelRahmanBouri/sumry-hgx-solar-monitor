from datetime import UTC, datetime, timedelta
import tempfile
import unittest
from unittest.mock import patch

from solar_monitor import battery, change_policy, propose
from solar_monitor import collector as collector_module
from solar_monitor.collector import Collector
from solar_monitor.config import SerialConfig
from solar_monitor.db import Database
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol, _crc16_modbus
from solar_monitor.serial_io import Exchange, _transact
from solar_monitor.web import create_app
from tests.test_battery import T0, readings
from tests.test_decode import CAPTURE_1
from tests.test_safety import REAL_CONFIG_REPLY


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f"{temp.name}/test.sqlite3")
        self.collector = Collector(SerialConfig(port="sim://"), self.db)
        write = patch.object(collector_module, "write_exchange", return_value=
                             Exchange("t", "sim://", 9600, "write_settings_0200", b"", b"", 0))
        write.start()
        self.addCleanup(write.stop)


class WriteBoundaryTests(DatabaseTestCase):
    def test_direct_write_is_refused_by_default_before_opening_port(self):
        with patch.object(collector_module, "exchange", return_value=
                          Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)) as read:
            with self.assertRaises(PermissionError):
                self.collector.apply_config({"buzzer": False}, settle_seconds=0)
        read.assert_not_called()

    def test_disabling_writes_after_approval_prevents_transmission(self):
        request_id = self.db.create_change_request(
            datetime.now(UTC).isoformat(), "admin", "test", {"buzzer": False}, status="approved",
        )
        with patch.object(collector_module, "exchange", return_value=
                          Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)) as read:
            self.collector.apply_approved_changes()
        read.assert_not_called()
        self.assertNotEqual(self.db.change_request(request_id)["status"], "applied")

    def test_switch_is_checked_again_after_reading_configuration(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")

        def read(*args):
            self.db.set_settings({"writes_enabled": "false"}, "now")
            return Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)

        with patch.object(collector_module, "exchange", read), \
             patch.object(collector_module, "write_exchange") as write:
            with self.assertRaises(PermissionError):
                self.collector.apply_config({"buzzer": False}, settle_seconds=0)
        write.assert_not_called()

    def test_ignored_one_register_step_is_not_verified(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        current = self.collector.protocol.decode(REAL_CONFIG_REPLY).metrics["bulk_charge_voltage_v"]
        with patch.object(collector_module, "exchange", return_value=
                          Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)):
            result = self.collector.apply_config({"bulk_charge_voltage_v": current - 0.01}, settle_seconds=0)
        self.assertFalse(result["verified"])
        self.assertIn("bulk_charge_voltage_v", result["mismatches"])

    def test_unrepresentable_values_cannot_be_rounded_outside_approval(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        for field, value in (("bulk_charge_voltage_v", 28.405), ("charge_cutoff_current_a", 5.04)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                propose.propose(self.db, {field: value}, "precision", "test")
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.collector.protocol.build_config_write(REAL_CONFIG_REPLY, {field: value})

    def test_policy_cannot_approve_while_writes_are_off(self):
        self.db.set_settings({
            change_policy.ENABLED_SETTING: "true",
            change_policy.POLICY_SETTING: '{"buzzer": [false]}',
        }, "now")
        result = propose.propose(self.db, {"buzzer": False}, "test", "test")
        self.assertFalse(result["automatic"])

    def test_revoked_automatic_policy_prevents_a_queued_write(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        request_id = self.db.create_change_request(
            datetime.now(UTC).isoformat(), "test", "test", {"buzzer": False},
            status="approved", decided_by="auto-apply policy",
        )
        with patch.object(collector_module, "exchange", return_value=
                          Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)) as read:
            self.collector.apply_approved_changes()
        read.assert_not_called()
        self.assertEqual(self.db.change_request(request_id)["status"], "failed")

    def test_old_approved_request_expires_without_opening_port(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        old = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
        request_id = self.db.create_change_request(old, "test", "test", {"buzzer": False}, status="approved")
        with patch.object(collector_module, "exchange", return_value=
                          Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0)) as read:
            self.collector.apply_approved_changes()
        read.assert_not_called()
        self.assertEqual(self.db.change_request(request_id)["status"], "expired")

    def test_stale_pending_request_cannot_be_approved_directly(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        old = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
        request_id = self.db.create_change_request(old, "test", "test", {"buzzer": False})
        client = create_app(self.db).test_client()
        with client.session_transaction() as session:
            session["admin_user"] = "admin"
        response = client.post(f"/api/admin/inverter/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.change_request(request_id)["status"], "expired")


class CapacityContinuityTests(unittest.TestCase):
    def test_second_rest_does_not_reuse_a_full_anchor(self):
        rows = readings(T0, 15, 27.2, 3)
        rows += readings(T0 + timedelta(minutes=15), 300, 24.8, -10)
        rows += readings(T0 + timedelta(minutes=315), 40, 24.36, 0)
        first = battery.count_charge(rows, 200)
        rows += readings(T0 + timedelta(minutes=355), 180, 24, -10)
        rows += readings(T0 + timedelta(minutes=535), 40, 23.64, 0)
        second = battery.count_charge(rows, 200)
        self.assertEqual(len(second["rest_events"]), 2)
        self.assertEqual(second["capacity_measurements"], first["capacity_measurements"])

    def test_absorption_cannot_span_missing_telemetry(self):
        rows = readings(T0, 4, 29.2, 10)
        rows += readings(T0 + timedelta(hours=3), 4, 29.2, 3)
        day = battery.daily_health(rows, 29.2)[0]
        self.assertFalse(day["absorption_reached"])
        self.assertIsNone(day["absorption_minutes"])

    def test_gap_after_an_absorption_hold_does_not_prove_it_completed(self):
        rows = readings(T0, 30, 29.2, 10)
        rows += readings(T0 + timedelta(hours=3), 4, 27.2, 3)
        day = battery.daily_health(rows, 29.2)[0]
        self.assertTrue(day["absorption_reached"])
        self.assertFalse(day["absorption_ended"])
        self.assertEqual(day["absorption_minutes"], 29)

    def test_unobserved_discharge_invalidates_capacity_learning(self):
        rows = readings(T0, 15, 27.2, 3)
        rows += readings(T0 + timedelta(minutes=15), 300, 24.8, -10)
        rows += readings(T0 + timedelta(minutes=600), 40, 24.36, 0)
        self.assertEqual(battery.count_charge(rows, 200)["capacity_measurements"], [])


class RestorationTests(unittest.TestCase):
    def test_unidentified_bits_can_only_be_restored_to_zero(self):
        protocol = Sumry8851Protocol()
        for value in ("1", "2", "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocol.build_config_write(REAL_CONFIG_REPLY, {"unknown_flag_byte8_bits34": value})


class HealthAndHistoryTests(DatabaseTestCase):
    def test_empty_or_corrupt_status_does_not_refresh_health(self):
        for response in (b"", b"noise", CAPTURE_1[:-1] + bytes([CAPTURE_1[-1] ^ 1])):
            with self.subTest(response=response[:5]):
                result = Exchange("t", "sim://", 9600, "status_candidate_0003", b"", response, 0)
                with patch.object(collector_module, "exchange", return_value=result):
                    self.assertFalse(self.collector.collect_once())
                self.assertIsNone(self.collector.last_success_at)
                self.assertGreater(self.collector.consecutive_failures, 0)

    def test_good_configuration_does_not_hide_bad_status(self):
        replies = [
            Exchange("t", "sim://", 9600, "status_candidate_0003", b"", b"", 0),
            Exchange("t", "sim://", 9600, "current_settings_0200", b"", REAL_CONFIG_REPLY, 0),
        ]
        with patch.object(collector_module, "exchange", side_effect=replies):
            self.assertFalse(self.collector.collect_once())
        self.assertIsNone(self.collector.last_success_at)
        self.assertEqual(self.collector.consecutive_failures, 1)

    def test_a_day_at_ten_second_polling_keeps_its_peak(self):
        start = datetime.now(UTC) - timedelta(hours=23)
        with self.db.connect() as connection:
            connection.executemany(
                "INSERT INTO samples (captured_at, pv_watts) VALUES (?, ?)",
                [((start + timedelta(seconds=10 * i)).isoformat(), 2500 if i == 1 else 100)
                 for i in range(8000)],
            )
        rows = create_app(self.db).test_client().get('/api/history?hours=24&points=10000').json
        self.assertEqual(len(rows), 8000)
        self.assertEqual(max(row['pv_watts'] for row in rows), 2500)


class AdminBoundaryTests(DatabaseTestCase):
    def test_simulator_cannot_call_host_controls(self):
        client = create_app(self.db, self.collector.config, self.collector).test_client()
        with client.session_transaction() as session:
            session["admin_user"] = "admin"
        with patch('solar_monitor.privileged._run') as helper:
            for action in ('reboot', 'restart-service', 'eco', 'wifi', 'update-check'):
                response = client.post('/api/admin/system/' + action, json={"enabled": True})
                self.assertEqual(response.status_code, 403, action)
            self.assertEqual(client.get('/api/admin/system/wifi').status_code, 403)
        helper.assert_not_called()

    def client(self):
        client = create_app(self.db).test_client()
        with client.session_transaction() as session:
            session["admin_user"] = "admin"
        return client

    def test_write_endpoints_reject_non_object_payloads(self):
        client = self.client()
        for route in ('settings', 'restore', 'writes', 'auto-apply'):
            for payload in ([1], "invalid", 1):
                with self.subTest(route=route, payload=payload):
                    self.assertEqual(client.post('/api/admin/inverter/' + route, json=payload).status_code, 400)

    def test_oversized_numbers_are_rejected_as_invalid_input(self):
        with self.assertRaises(ValueError):
            Sumry8851Protocol().build_config_write(REAL_CONFIG_REPLY, {"bulk_charge_voltage_v": 10 ** 400})
        self.assertFalse(change_policy.evaluate({"bulk_charge_voltage_v": 10 ** 400},
                                                {"bulk_charge_voltage_v": {"min": 24, "max": 29.2}})[0])

    def test_string_false_cannot_enable_writes_or_automation(self):
        client = self.client()
        for route in ('writes', 'auto-apply'):
            response = client.post('/api/admin/inverter/' + route, json={"enabled": "false"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.get_settings()['writes_enabled'], 'false')

    def test_malformed_policy_fails_closed(self):
        for rule in ({}, {"minimum": 28}, {"min": "NaN"}, {"max": "bad"}, {"min": None}):
            with self.subTest(rule=rule):
                self.assertFalse(change_policy.evaluate({"bulk_charge_voltage_v": 29.2},
                                                        {"bulk_charge_voltage_v": rule})[0])

    def test_policy_endpoint_refuses_unbounded_or_wider_ranges(self):
        import json
        client = self.client()
        for rule in ({}, {"min": 20}, {"min": "NaN"}, {"min": 29, "max": 28}, [], [True]):
            response = client.post('/api/admin/inverter/auto-apply', json={
                "enabled": True, "policy": json.dumps({"bulk_charge_voltage_v": rule}),
            })
            self.assertEqual(response.status_code, 400, rule)

    def test_cross_origin_cannot_toggle_writes_with_an_admin_cookie(self):
        client = self.client()
        response = client.post('/api/admin/inverter/writes', json={"enabled": True},
                               headers={"Origin": "https://unrelated.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.get_settings()['writes_enabled'], 'false')
        response = client.post('/api/admin/inverter/writes', json={"enabled": True},
                               headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)

    def test_admin_responses_cannot_be_cached_or_framed(self):
        response = self.client().get('/admin')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Content-Security-Policy'], "frame-ancestors 'none'")


class TransportBoundaryTests(unittest.TestCase):
    def test_a_continuous_reply_stops_at_the_buffer_limit(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.in_waiting = 8192
        sizes = []

        def read(size):
            sizes.append(size)
            if len(sizes) > 10:
                raise AssertionError("serial read did not stop at the buffer limit")
            return b"x" * size

        connection.read.side_effect = read
        with patch('solar_monitor.serial_io.serial.serial_for_url', return_value=connection), \
             patch('solar_monitor.serial_io.time.sleep'):
            result = _transact('fake', 9600, 1, 'test', b'x', 0)
        self.assertEqual(len(result.response), 4096)
        self.assertEqual(sizes, [512, 3584])

    def test_a_forged_in_range_mask_cannot_exceed_the_field_range(self):
        protocol = Sumry8851Protocol()
        frame = bytearray(protocol.build_config_write(REAL_CONFIG_REPLY, {"bulk_charge_voltage_v": 28.4}))
        frame[48:50] = (3100).to_bytes(2, 'little')
        frame[-2:] = _crc16_modbus(frame[:-2]).to_bytes(2, 'little')
        with self.assertRaises(PermissionError):
            protocol.assert_write_frame(bytes(frame), REAL_CONFIG_REPLY)

    def test_status_header_is_checked_even_when_checksum_passes(self):
        frame = bytearray(CAPTURE_1)
        frame[2:4] = b'\x00\x10'
        frame[-2:] = _crc16_modbus(frame[:-2]).to_bytes(2, 'little')
        self.assertFalse(Sumry8851Protocol().decode(bytes(frame)).valid)
