"""The proposal CLI must queue valid intent without accessing a serial device."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solar_monitor import change_policy, propose
from solar_monitor.db import Database
from solar_monitor.protocols.base import Protocol
from solar_monitor.protocols.sumry_8851 import Sumry8851Protocol
from solar_monitor.web import create_app


class ProposalCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.db = Database(self.directory / 'queue.sqlite3')
        self.config = self.directory / 'config.toml'
        self.config.write_text('[serial]\nport = "sim://"\n[storage]\ndatabase = '
                               + json.dumps(str(self.db.path)) + '\n')
        port = patch('solar_monitor.serial_io.serial.serial_for_url',
                     side_effect=AssertionError('proposal opened a port'))
        self.port = port.start()
        self.addCleanup(port.stop)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = propose.main(['--config', str(self.config), *args])
            except SystemExit as exc:
                code = exc.code
        self.port.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_valid_intent_is_pending_with_actor_reason_and_typed_values(self):
        code, output, errors = self.run_cli('--set', 'buzzer=off', '--set', 'bulk_charge_voltage_v=28.4',
                                            '--set', 'equalization_interval_days=30',
                                            '--reason', 'check settings', '--by', 'operator')
        self.assertEqual((code, errors), (0, ''))
        self.assertIn('waiting for approval', output)
        row = self.db.change_requests(1)[0]
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['requested_by'], 'operator')
        self.assertEqual(row['reason'], 'check settings')
        self.assertEqual(row['changes'], {'buzzer': False, 'bulk_charge_voltage_v': 28.4,
                                          'equalization_interval_days': 30})
        self.assertEqual(self.db.recent_audit(1)[0]['action'], 'inverter.change.proposed')

    def test_policy_approval_is_queued_but_never_sent_by_cli(self):
        self.db.set_settings({'writes_enabled': 'true', change_policy.ENABLED_SETTING: 'true',
                             change_policy.POLICY_SETTING: '{"buzzer": [true]}'}, 'now')
        code, output, _ = self.run_cli('--set', 'buzzer=yes', '--reason', 'approved envelope')
        self.assertEqual(code, 0)
        self.assertIn('approved by policy', output)
        self.assertEqual(self.db.change_requests(1)[0]['status'], 'approved')
        self.assertEqual(self.db.raw_frames(), [])

    def test_invalid_arguments_leave_no_request(self):
        cases = [(), ('--set', 'buzzer'), ('--set', 'buzzer=off'),
                 ('--set', 'unknown=1', '--reason', 'bad field'),
                 ('--set', 'bulk_charge_voltage_v=NaN', '--reason', 'bad value'),
                 ('--set', 'bulk_charge_voltage_v=not-a-number', '--reason', 'bad value')]
        for args in cases:
            with self.subTest(args=args):
                code, _, error = self.run_cli(*args)
                self.assertIn(code, (1, 2))
                self.assertTrue(error)
                self.assertEqual(self.db.change_requests(), [])

    def test_missing_config_is_a_usage_error(self):
        self.config.unlink()
        code, _, error = self.run_cli('--list')
        self.assertEqual(code, 2)
        self.assertIn(str(self.config), error)

    def test_list_distinguishes_verified_failed_and_unverified_outcomes(self):
        for status, result in (('pending', None), ('applied', {'verified': True}),
                               ('failed', {'error': 'disconnected'}), ('unverified', {'verified': False})):
            request_id = self.db.create_change_request(datetime.now(UTC).isoformat(), 'operator', 'test', {'buzzer': False})
            if result is not None:
                self.db.finish_change_request(request_id, status, 'now', result)
        code, output, error = self.run_cli('--list')
        self.assertEqual((code, error), (0, ''))
        for value in ('pending', 'applied', 'verified', 'failed', 'disconnected', 'not verified'):
            self.assertIn(value, output)

    def test_unconfirmed_protocol_cannot_queue_writes(self):
        with patch('solar_monitor.propose.create_protocol', return_value=Protocol()), \
             self.assertRaisesRegex(ValueError, 'does not support writes'):
            propose.propose(self.db, {'buzzer': False}, 'test', 'operator')
        self.assertEqual(self.db.change_requests(), [])


class PolicyBoundaryTests(unittest.TestCase):
    def test_empty_proposal_and_scalar_rules_never_auto_approve(self):
        self.assertFalse(change_policy.evaluate({}, {'buzzer': [False]})[0])
        for rule in (False, 28, '28.4'):
            self.assertFalse(change_policy.evaluate({'bulk_charge_voltage_v': 28.4},
                                                    {'bulk_charge_voltage_v': rule})[0])

    def test_policy_validation_accepts_exact_choices_and_one_sided_ranges(self):
        from dataclasses import asdict
        fields = {name: asdict(field) for name, field in Sumry8851Protocol.writable_fields.items()}
        for policy in ({'buzzer': [True, False]}, {'bulk_charge_voltage_v': [28, 28.4]},
                       {'bulk_charge_voltage_v': {'min': 28}}, {'bulk_charge_voltage_v': {'max': 28.4}}):
            change_policy.validate_policy(policy, fields)
        for policy in ({'made_up': [1]}, {'buzzer': ['maybe']}):
            with self.assertRaises(ValueError):
                change_policy.validate_policy(policy, fields)

    def test_inverter_without_a_collector_cannot_execute_a_write(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'test.sqlite3')
            client = create_app(db).test_client()
            with client.session_transaction() as session:
                session['admin_user'] = 'admin'
            response = client.post('/api/admin/inverter/settings', json={'changes': {'buzzer': False}})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(db.raw_frames(), [])
