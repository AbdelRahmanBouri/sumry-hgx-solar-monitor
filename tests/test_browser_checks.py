"""The browser check must fail even when application code catches a failure."""
import unittest
from unittest.mock import Mock, patch

from tools.check_demo import check_write, require
from tools.screenshot import Browser, page_errors


class BrowserCheckTests(unittest.TestCase):
    def test_api_error_cannot_hide_behind_a_successful_page_load(self):
        events = [{"sessionId": "page", "method": "Network.responseReceived", "params": {
            "type": "Fetch", "response": {"status": 500, "url": "http://localhost/api/history"},
        }}]
        self.assertEqual(page_errors(events, "page"), ["HTTP 500: http://localhost/api/history"])
        self.assertEqual(page_errors(events, "another-page"), [])

    def test_broken_scripts_and_failed_connections_are_reported(self):
        events = [
            {"sessionId": "page", "method": "Runtime.exceptionThrown", "params": {
                "exceptionDetails": {"exception": {"description": "ReferenceError: missing"}},
            }},
            {"sessionId": "page", "method": "Network.loadingFailed", "params": {
                "errorText": "net::ERR_CONNECTION_REFUSED",
            }},
        ]
        self.assertEqual(len(page_errors(events, "page")), 2)

    def test_security_policy_violations_are_not_silently_ignored(self):
        events = [{"sessionId": "page", "method": "Log.entryAdded", "params": {
            "entry": {"source": "security", "text": "Blocked inline script"}}}]
        self.assertEqual(page_errors(events, "page"), ["Blocked inline script"])

    def test_evaluated_action_exception_is_not_a_successful_protocol_reply(self):
        browser = Browser.__new__(Browser)
        browser.send = Mock(return_value={"exceptionDetails": {"text": "missing button"}})
        with self.assertRaisesRegex(RuntimeError, "missing button"):
            browser.evaluate("document.querySelector('missing').click()", "page")

    def test_readiness_requires_a_populated_page(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = Mock(return_value=False)
        with patch('tools.screenshot.time.monotonic', side_effect=[0, 0, 16]), \
             patch('tools.screenshot.time.sleep'), self.assertRaises(TimeoutError):
            browser.wait_until("historyRows.length > 0", "page")

    def test_load_event_from_another_page_does_not_release_the_wait(self):
        browser = Browser.__new__(Browser)
        browser.events = [{"method": "Page.loadEventFired", "sessionId": "old"}]
        browser._message = Mock(return_value={"method": "Page.loadEventFired", "sessionId": "new"})
        browser.wait_for("Page.loadEventFired", session="new")
        browser._message.assert_called_once()


class DemoCleanupTests(unittest.TestCase):
    def test_failed_verification_still_restores_the_value_and_disables_writes(self):
        calls = []

        def call(path, body=None):
            calls.append((path, body))
            if path == '/api/admin/inverter':
                return {"current": {"settings": {"charge_cutoff_current_a": 4}}}
            if path.endswith('/settings'):
                value = body['changes']['charge_cutoff_current_a']
                return {"verified": value == 4, "after": {"charge_cutoff_current_a": value}}
            return {}

        with self.assertRaisesRegex(AssertionError, "not verified"):
            check_write(call)
        self.assertEqual(calls[-2][1], {"changes": {"charge_cutoff_current_a": 4}})
        self.assertEqual(calls[-1][1], {"enabled": False})

    def test_smoke_assertions_remain_active_under_optimized_python(self):
        with self.assertRaises(AssertionError):
            require(False)
