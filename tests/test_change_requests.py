"""Proposals are intents, not writes.

The property under test throughout: a proposal reaches the serial port only
after something released it -- a person approving, or a policy an admin wrote
deliberately -- and it is applied by the process that owns the port, exactly
once.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solar_monitor import change_policy, propose
from solar_monitor import collector as collector_module
from solar_monitor.collector import Collector
from solar_monitor.config import SerialConfig
from solar_monitor.db import Database
from solar_monitor.serial_io import Exchange
from solar_monitor.web import create_app
from tests.test_safety import REAL_CONFIG_REPLY, as_reply


class PolicyTests(unittest.TestCase):
    def test_nothing_is_automatic_without_a_policy(self):
        allowed, why = change_policy.evaluate({"buzzer": "false"}, {})
        self.assertFalse(allowed)
        self.assertIn("switched off", why[0])

    def test_a_field_outside_the_policy_blocks_the_whole_proposal(self):
        policy = {"bulk_charge_voltage_v": {"min": 28.0, "max": 29.2}}
        allowed, why = change_policy.evaluate(
            {"bulk_charge_voltage_v": 28.4, "low_battery_cutoff_v": 22.0}, policy
        )
        self.assertFalse(allowed, "one uncovered field must send the lot to a person")
        self.assertEqual(len(why), 1)
        self.assertIn("low_battery_cutoff_v", why[0])

    def test_values_outside_the_range_are_blocked(self):
        policy = {"bulk_charge_voltage_v": {"min": 28.0, "max": 29.2}}
        self.assertFalse(change_policy.evaluate({"bulk_charge_voltage_v": 29.3}, policy)[0])
        self.assertFalse(change_policy.evaluate({"bulk_charge_voltage_v": 27.9}, policy)[0])
        self.assertTrue(change_policy.evaluate({"bulk_charge_voltage_v": 28.4}, policy)[0])

    def test_choices_are_matched_by_membership(self):
        policy = {"overload_auto_restart": [True]}
        self.assertTrue(change_policy.evaluate({"overload_auto_restart": True}, policy)[0])
        self.assertFalse(change_policy.evaluate({"overload_auto_restart": False}, policy)[0])

    def test_numeric_choices_match_without_treating_switches_as_numbers(self):
        self.assertTrue(change_policy.evaluate({"bulk_charge_voltage_v": 28.0},
                                              {"bulk_charge_voltage_v": [28]})[0])
        self.assertFalse(change_policy.evaluate({"equalization_interval_days": True},
                                               {"equalization_interval_days": [1]})[0])

    def test_the_policy_is_ignored_unless_switched_on(self):
        settings = {change_policy.POLICY_SETTING: '{"buzzer": [true, false]}'}
        self.assertEqual(change_policy.load_policy(settings), {})
        settings[change_policy.ENABLED_SETTING] = "true"
        self.assertIn("buzzer", change_policy.load_policy(settings))

    def test_a_broken_policy_fails_closed(self):
        settings = {change_policy.ENABLED_SETTING: "true", change_policy.POLICY_SETTING: "{not json"}
        self.assertEqual(change_policy.load_policy(settings), {})


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_a_proposal_waits_by_default(self):
        outcome = propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "testing", "automation")
        self.assertFalse(outcome["automatic"])
        self.assertEqual(self.db.change_request(outcome["id"])["status"], "pending")

    def test_an_unwritable_field_is_refused_at_proposal_time(self):
        for changes in ({"output_voltage_v": 110}, {"bulk_charge_voltage_v": 31},
                        {"equalization_interval_days": True}, {}):
            with self.assertRaises(ValueError):
                propose.propose(self.db, changes, "testing", "automation")
        self.assertEqual(self.db.change_requests(10), [])

    def test_the_policy_can_release_a_proposal(self):
        self.db.set_settings({"writes_enabled": "true"}, "now")
        self.db.set_settings({
            change_policy.ENABLED_SETTING: "true",
            change_policy.POLICY_SETTING: '{"bulk_charge_voltage_v": {"min": 28.0, "max": 29.2}}',
        }, "now")
        outcome = propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "testing", "automation")
        self.assertTrue(outcome["automatic"])
        request = self.db.change_request(outcome["id"])
        self.assertEqual(request["status"], "approved")
        self.assertEqual(request["decided_by"], "auto-apply policy")

    def test_a_stale_proposal_expires_rather_than_waiting_forever(self):
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        stale = self.db.create_change_request(old, "automation", "old", {"buzzer": "false"})
        propose.propose(self.db, {"buzzer": "true"}, "fresh", "automation")
        self.assertEqual(self.db.change_request(stale)["status"], "expired")

    def test_a_decision_cannot_be_replayed(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        self.assertTrue(self.db.decide_change_request(request_id, "approved", "admin", "now"))
        self.assertFalse(self.db.decide_change_request(request_id, "rejected", "admin", "now"))
        self.assertEqual(self.db.change_request(request_id)["status"], "approved")

    def test_a_request_is_claimed_exactly_once(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        self.db.decide_change_request(request_id, "approved", "admin", "now")
        self.assertTrue(self.db.claim_change_request(request_id))
        self.assertFalse(self.db.claim_change_request(request_id), "a claimed request must not be reclaimed")


class CollectorApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")
        self.db.set_settings({"writes_enabled": "true"}, "now")
        self.collector = Collector(SerialConfig(port="auto"), self.db)
        self.collector._port = lambda: "/dev/fake"

    def tearDown(self):
        self.temp.cleanup()

    def _wire(self, sent):
        replies = {"n": 0}

        def fake_exchange(port, baud, timeout, protocol, query, settle_seconds=0.15):
            replies["n"] += 1
            body = REAL_CONFIG_REPLY if replies["n"] == 1 or not sent else as_reply(sent[0])
            return Exchange("t", port, baud, query.name, query.frame, body, 5)

        def fake_write(port, baud, timeout, protocol, frame, template, settle_seconds=0.15):
            protocol.assert_write_frame(frame, template)
            sent.append(frame)
            return Exchange("t", port, baud, protocol.write_query_name, frame, b"", 5)

        return patch.object(collector_module, "exchange", fake_exchange), \
            patch.object(collector_module, "write_exchange", fake_write)

    def test_only_approved_requests_are_written(self):
        propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "waiting", "automation")
        sent = []
        read, write = self._wire(sent)
        with read, write:
            self.assertEqual(self.collector.apply_approved_changes(), [])
        self.assertEqual(sent, [], "a pending request must never reach the port")

    def test_an_approved_request_is_written_and_verified(self):
        request_id = propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "testing", "automation")["id"]
        self.db.decide_change_request(request_id, "approved", "admin", "now")
        sent = []
        read, write = self._wire(sent)
        with read, write:
            outcomes = self.collector.apply_approved_changes()
        self.assertEqual(len(sent), 1)
        self.assertEqual(outcomes[0]["status"], "applied")
        record = self.db.change_request(request_id)
        self.assertEqual(record["status"], "applied")
        self.assertTrue(record["result"]["verified"])
        self.assertEqual(record["result"]["after"]["bulk_charge_voltage_v"], 28.4)

    def test_a_request_the_inverter_ignores_is_marked_unverified(self):
        request_id = propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "testing", "automation")["id"]
        self.db.decide_change_request(request_id, "approved", "admin", "now")

        def stubborn(port, baud, timeout, protocol, query, settle_seconds=0.15):
            return Exchange("t", port, baud, query.name, query.frame, REAL_CONFIG_REPLY, 5)

        with patch.object(collector_module, "exchange", stubborn), \
             patch.object(collector_module, "write_exchange",
                          lambda *a, **k: Exchange("t", "p", 9600, "w", b"", b"", 5)):
            self.collector.apply_approved_changes()
        record = self.db.change_request(request_id)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("bulk_charge_voltage_v", record["result"]["mismatches"])

    def test_the_same_request_is_never_applied_twice(self):
        request_id = propose.propose(self.db, {"bulk_charge_voltage_v": 28.4}, "testing", "automation")["id"]
        self.db.decide_change_request(request_id, "approved", "admin", "now")
        sent = []
        read, write = self._wire(sent)
        with read, write:
            self.collector.apply_approved_changes()
            self.collector.apply_approved_changes()
        self.assertEqual(len(sent), 1)


class ApprovalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.temp.name}/test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _client(self):
        client = create_app(self.db).test_client()
        token = Path(f"{self.temp.name}/setup-token").read_text(encoding="utf-8")
        client.post(f"/admin/setup?token={token}", data={
            "token": token, "username": "admin",
            "password": "correct-horse-1", "confirmation": "correct-horse-1"})
        return client

    def test_approval_requires_a_login(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        anonymous = create_app(self.db).test_client()
        self.assertEqual(anonymous.post(f"/api/admin/inverter/requests/{request_id}/approve").status_code, 401)
        self.assertEqual(self.db.change_request(request_id)["status"], "pending")

    def test_approval_requires_writes_to_be_enabled(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        client = self._client()
        response = client.post(f"/api/admin/inverter/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.change_request(request_id)["status"], "pending")

    def test_rejecting_records_who_decided(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        client = self._client()
        response = client.post(f"/api/admin/inverter/requests/{request_id}/reject")
        self.assertEqual(response.status_code, 200)
        record = self.db.change_request(request_id)
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["decided_by"], "admin")

    def test_a_decided_request_cannot_be_decided_again(self):
        request_id = propose.propose(self.db, {"buzzer": "false"}, "testing", "automation")["id"]
        client = self._client()
        client.post(f"/api/admin/inverter/requests/{request_id}/reject")
        self.assertEqual(
            client.post(f"/api/admin/inverter/requests/{request_id}/approve").status_code, 409
        )

    def test_the_policy_endpoint_rejects_unwritable_fields_and_bad_json(self):
        client = self._client()
        self.assertEqual(client.post("/api/admin/inverter/auto-apply",
                                     json={"enabled": True, "policy": "{oops"}).status_code, 400)
        self.assertEqual(client.post("/api/admin/inverter/auto-apply",
                                     json={"enabled": True, "policy": '{"output_voltage_v": [110]}'}).status_code, 400)
        ok = client.post("/api/admin/inverter/auto-apply",
                         json={"enabled": True, "policy": '{"buzzer": [true, false]}'})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json["auto_apply"])


if __name__ == "__main__":
    unittest.main()
