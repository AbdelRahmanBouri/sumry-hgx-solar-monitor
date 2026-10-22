from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from solar_monitor.db import Database
from solar_monitor.security import LoginLimiter
from solar_monitor.web import create_app


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'security.sqlite3')
        self.db.create_admin('admin', generate_password_hash('correct-password'), 'now')
        self.app = create_app(self.db)
        self.client = self.app.test_client()

    def login(self, client=None, **kwargs):
        return (client or self.client).post('/admin/login', data={
            'username': 'admin', 'password': 'correct-password'}, **kwargs)

    def test_headers_cover_pages_assets_api_and_errors(self):
        for path in ('/', '/estimate', '/admin/login', '/static/app.js', '/api/status', '/missing'):
            with self.subTest(path=path):
                response = self.client.get(path)
                csp = response.headers['Content-Security-Policy']
                for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                                  "style-src-attr 'none'", "form-action 'self'", "base-uri 'none'"):
                    self.assertIn(directive, csp)
                self.assertNotIn('unsafe-inline', csp)
                self.assertNotIn('unsafe-eval', csp)
                self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
                self.assertEqual(response.headers['Permissions-Policy'],
                                 'camera=(), microphone=(), geolocation=(self)')
                response.close()
        self.assertEqual(self.client.get('/admin/login').headers['Cache-Control'], 'no-store')
        self.assertNotIn('Strict-Transport-Security', self.client.get('/').headers)
        self.assertEqual(self.client.get('/', base_url='https://localhost').headers[
            'Strict-Transport-Security'], 'max-age=31536000')

    def test_cookie_flags_follow_transport_and_explicit_proxy_configuration(self):
        for secure, base in ((False, 'http://localhost'), (False, 'https://localhost'),
                             (True, 'http://localhost')):
            with self.subTest(secure=secure, base=base):
                client = create_app(self.db, secure_cookies=secure).test_client()
                response = self.login(client, base_url=base)
                self.assertEqual(response.status_code, 302)
                cookie = response.headers['Set-Cookie']
                self.assertIn('HttpOnly', cookie)
                self.assertIn('SameSite=Strict', cookie)
                self.assertIn('Path=/', cookie)
                self.assertEqual('Secure;' in cookie, secure or base.startswith('https:'))
                self.assertNotIn('Domain=', cookie)
                # An idle session is not extended by every dashboard request.
                self.assertNotIn('Set-Cookie', client.get('/', base_url=base).headers)

    def test_forwarded_headers_do_not_spoof_transport_or_bypass_source_budget(self):
        for index in range(20):
            response = self.client.post('/admin/login', data={'username': f'unknown{index}', 'password': 'bad'},
                                        headers={'X-Forwarded-For': f'192.0.2.{index}', 'X-Forwarded-Proto': 'https'})
            self.assertEqual(response.status_code, 200)
        with patch('solar_monitor.web.check_password_hash') as check:
            response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['Retry-After']), 0)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        check.assert_not_called()
        # It is still HTTP, so an arbitrary forwarded header cannot set HSTS.
        self.assertNotIn('Strict-Transport-Security', response.headers)

    def test_account_budget_survives_app_restart_and_rotating_sources(self):
        for index in range(5):
            self.client.post('/admin/login', data={'username': 'admin', 'password': 'bad'},
                             environ_overrides={'REMOTE_ADDR': f'192.0.2.{index}'})
        restarted = create_app(self.db).test_client()
        with patch('solar_monitor.web.check_password_hash') as check:
            response = self.login(restarted, environ_overrides={'REMOTE_ADDR': '198.51.100.1'})
        self.assertEqual(response.status_code, 429)
        check.assert_not_called()

    def test_unknown_accounts_are_limited_and_pin_setup_share_source_budget(self):
        for _ in range(5):
            self.client.post('/admin/login', data={'username': 'unknown', 'password': 'bad'})
        self.assertEqual(self.client.post('/admin/login', data={'username': 'unknown'}).status_code, 429)
        for _ in range(14):
            self.client.post('/admin/pin-login')
        self.assertEqual(self.client.post('/admin/setup').status_code, 429)

    def test_budget_reservations_are_atomic_and_expire(self):
        limiter = LoginLimiter(self.db, window_seconds=60)
        with patch('solar_monitor.security.wall_time', return_value=1000):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: limiter.take('test', 'same', 5), range(20)))
            self.assertEqual(results.count(0), 5)
            self.assertEqual(results.count(60), 15)
        with patch('solar_monitor.security.wall_time', return_value=1061):
            self.assertEqual(limiter.take('test', 'same', 5), 0)
            limiter.clear('test', 'same')
        with self.db.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM login_limits').fetchone()[0], 0)

    def test_global_budget_bounds_identity_spray(self):
        for index in range(100):
            self.client.post('/admin/login', data={'username': f'unknown{index}'},
                             environ_overrides={'REMOTE_ADDR': f'192.0.2.{index}'})
        response = self.login(environ_overrides={'REMOTE_ADDR': '198.51.100.2'})
        self.assertEqual(response.status_code, 429)
        with self.db.connect() as connection:
            rows = connection.execute('SELECT key FROM login_limits').fetchall()
        self.assertLessEqual(len(rows), 201)
        self.assertTrue(all(len(row[0]) == 64 for row in rows))
