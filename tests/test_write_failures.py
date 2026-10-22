"""Exercise failure outcomes at the queue, collector and HTTP write boundaries."""
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solar_monitor import change_policy, propose
from solar_monitor.collector import Collector
from solar_monitor.config import SerialConfig
from solar_monitor.db import Database
from solar_monitor.protocols.base import Protocol
from solar_monitor.serial_io import Exchange
from solar_monitor.web import create_app
from tests.test_safety import REAL_CONFIG_REPLY, as_reply


class WriteFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / 'test.sqlite3')
        self.db.set_settings({'writes_enabled': 'true'}, 'now')
        self.collector = Collector(SerialConfig(port='sim://'), self.db)
        port = patch('solar_monitor.serial_io.serial.serial_for_url',
                     side_effect=AssertionError('unit test opened a serial port'))
        self.open_port = port.start()
        self.addCleanup(port.stop)
        sleep = patch('solar_monitor.collector.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def reply(self, response=REAL_CONFIG_REPLY, error=None):
        return Exchange(datetime.now(UTC).isoformat(), 'sim://', 9600, 'current_settings_0200',
                        b'', response, 1, error)

    def approved(self):
        return self.db.create_change_request(datetime.now(UTC).isoformat(), 'test', 'failure check',
                                             {'buzzer': False}, status='approved', decided_by='admin')

    def test_failed_transmission_is_audited_and_never_automatically_retried(self):
        request_id = self.approved()
        with patch('solar_monitor.collector.exchange', return_value=self.reply()) as read, \
             patch('solar_monitor.collector.write_exchange', return_value=self.reply(b'', 'adapter disconnected')) as write:
            result = self.collector.apply_approved_changes()[0]
            self.assertEqual(result['status'], 'failed')
            self.assertIn('adapter disconnected', result['result']['error'])
            self.assertEqual(self.collector.apply_approved_changes(), [])
        read.assert_called_once()
        write.assert_called_once()
        self.assertEqual(self.db.change_request(request_id)['status'], 'failed')
        self.assertEqual(self.db.recent_audit(1)[0]['action'], 'inverter.change.failed')
        raw = self.db.raw_frames(1)[0]
        self.assertEqual(raw['decoded'], 0)
        self.assertEqual(raw['error'], 'adapter disconnected')

    def test_missing_readback_is_not_reported_as_applied_or_retried(self):
        request_id = self.approved()
        with patch('solar_monitor.collector.exchange', side_effect=[self.reply(), self.reply(b'')]) as read, \
             patch('solar_monitor.collector.write_exchange', return_value=self.reply(b'')) as write:
            result = self.collector.apply_approved_changes()[0]
            self.assertEqual(result['status'], 'failed')
            self.assertIn('write was sent', result['result']['error'])
            self.assertEqual(self.collector.apply_approved_changes(), [])
        self.assertEqual(read.call_count, 2)
        write.assert_called_once()
        self.assertEqual(self.db.change_request(request_id)['status'], 'failed')
        self.assertTrue(self.collector.lock.acquire(blocking=False), 'failed write left the port locked')
        self.collector.lock.release()

    def test_automatic_approval_is_rechecked_and_verified_by_the_collector(self):
        self.db.set_settings({change_policy.ENABLED_SETTING: 'true',
                             change_policy.POLICY_SETTING: '{"buzzer": [false]}'}, 'now')
        request_id = propose.propose(self.db, {'buzzer': False}, 'policy test', 'operator')['id']
        after = as_reply(self.collector.protocol.build_config_write(REAL_CONFIG_REPLY, {'buzzer': False}))
        with patch('solar_monitor.collector.exchange', side_effect=[self.reply(), self.reply(after)]), \
             patch('solar_monitor.collector.write_exchange', return_value=self.reply(b'')) as write:
            result = self.collector.apply_approved_changes()[0]
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['result']['verified'])
        self.assertEqual(self.db.change_request(request_id)['decided_by'], 'auto-apply policy')
        write.assert_called_once()

    def test_policy_revoked_during_template_read_prevents_transmission(self):
        self.db.set_settings({change_policy.ENABLED_SETTING: 'true',
                             change_policy.POLICY_SETTING: '{"buzzer": [false]}'}, 'now')
        propose.propose(self.db, {'buzzer': False}, 'revocation test', 'operator')

        def read(*args):
            self.db.set_settings({change_policy.ENABLED_SETTING: 'false'}, 'now')
            return self.reply()

        with patch('solar_monitor.collector.exchange', side_effect=read), \
             patch('solar_monitor.collector.write_exchange') as write:
            result = self.collector.apply_approved_changes()[0]
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Automatic approval no longer applies', result['result']['error'])
        write.assert_not_called()

    def test_losing_a_claim_never_reaches_the_serial_port(self):
        request_id = self.approved()
        with patch.object(self.db, 'claim_change_request', return_value=False), \
             patch.object(self.collector, 'apply_config') as apply:
            self.assertEqual(self.collector.apply_approved_changes(), [])
        apply.assert_not_called()
        self.assertEqual(self.db.change_request(request_id)['status'], 'approved')

    def test_unsupported_protocol_refuses_before_any_serial_io(self):
        self.collector.protocol = Protocol()
        with self.assertRaisesRegex(ValueError, 'does not support writes'):
            self.collector.apply_config({'buzzer': False})
        self.open_port.assert_not_called()

    def test_switch_strings_verify_against_decoded_values(self):
        # Exercise the actual encode/decode path, including string-valued form inputs.
        changes = {'buzzer': 'false', 'overtemperature_auto_restart': 'true',
                   'unknown_flag_byte8_bits34': 0}
        after = as_reply(self.collector.protocol.build_config_write(REAL_CONFIG_REPLY, changes))
        with patch('solar_monitor.collector.exchange', side_effect=[self.reply(), self.reply(after)]), \
             patch('solar_monitor.collector.write_exchange', return_value=self.reply(b'')):
            result = self.collector.apply_config(changes, settle_seconds=0)
        self.assertTrue(result['verified'])
        self.assertFalse(result['after']['buzzer'])
        self.assertTrue(result['after']['overtemperature_auto_restart'])

    def client(self):
        client = create_app(self.db, self.collector.config, self.collector).test_client()
        with client.session_transaction() as session:
            session['admin_user'] = 'admin'
        return client

    def test_http_failure_is_audited_without_claiming_a_success(self):
        with patch.object(self.collector, 'apply_config', side_effect=PermissionError('permission revoked')):
            response = self.client().post('/api/admin/inverter/settings', json={'changes': {'buzzer': False}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error'], 'permission revoked')
        event = self.db.recent_audit(1)[0]
        self.assertEqual(event['action'], 'inverter.write_failed')
        self.assertEqual(json.loads(event['detail'])['changes'], {'buzzer': False})

    def test_snapshot_restore_filters_read_only_fields_and_uses_the_same_write_gate(self):
        self.db.save_inverter_config(datetime.now(UTC).isoformat(), {'buzzer': False, 'output_voltage_v': 120})
        snapshot = self.db.inverter_config_changes(1)[0]
        client = self.client()
        with patch.object(self.collector, 'apply_config', return_value={'verified': True, 'mismatches': {}}) as apply:
            response = client.post('/api/admin/inverter/restore', json={'snapshot_id': snapshot['id']})
            self.assertEqual(response.status_code, 200)
            apply.assert_called_once_with({'buzzer': False})
            self.db.set_settings({'writes_enabled': 'false'}, 'now')
            self.assertEqual(client.post('/api/admin/inverter/restore',
                                         json={'snapshot_id': snapshot['id']}).status_code, 403)
            self.assertEqual(apply.call_count, 1)

    def test_invalid_snapshot_or_empty_write_never_reaches_collector(self):
        client = self.client()
        with patch.object(self.collector, 'apply_config') as apply:
            for value in ('bad', None, 999):
                self.assertEqual(client.post('/api/admin/inverter/restore',
                                             json={'snapshot_id': value}).status_code, 404)
            for body in ({}, {'changes': {}}, {'changes': {'output_voltage_v': 110}}):
                self.assertEqual(client.post('/api/admin/inverter/settings', json=body).status_code, 400)
        apply.assert_not_called()

    def test_invalid_approval_and_competing_decision_never_apply(self):
        client = self.client()
        request_id = self.db.create_change_request(datetime.now(UTC).isoformat(), 'test', 'test', {'buzzer': False})
        with patch.object(self.collector, 'apply_approved_changes') as apply:
            self.assertEqual(client.post(f'/api/admin/inverter/requests/{request_id}/other').status_code, 404)
            self.assertEqual(client.post('/api/admin/inverter/requests/999/approve').status_code, 404)
            with patch.object(self.db, 'decide_change_request', return_value=False):
                self.assertEqual(client.post(f'/api/admin/inverter/requests/{request_id}/approve').status_code, 409)
        apply.assert_not_called()

    def test_invalid_policy_does_not_replace_the_current_policy(self):
        client = self.client()
        original = '{"buzzer": [false]}'
        self.db.set_settings({change_policy.POLICY_SETTING: original}, 'now')
        for value in ([], '[]', '{"buzzer": ["maybe"]}', '{"buzzer": true}'):
            response = client.post('/api/admin/inverter/auto-apply', json={'enabled': True, 'policy': value})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.db.get_settings()[change_policy.POLICY_SETTING], original)

    def test_malformed_origin_and_cross_site_requests_cannot_change_writes(self):
        client = self.client()
        for headers in ({'Origin': 'http://['}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(client.post('/api/admin/inverter/writes', json={'enabled': False},
                                         headers=headers).status_code, 403)
        self.assertEqual(self.db.get_settings()['writes_enabled'], 'true')
