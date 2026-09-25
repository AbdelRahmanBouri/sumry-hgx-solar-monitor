from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from solar_monitor import privileged

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "pi-control.sh"


class WifiPassphraseTests(unittest.TestCase):
    """sudo writes the full command line of everything it runs to the system
    journal, so a passphrase passed as an argument is a passphrase on disk."""

    def test_the_passphrase_is_never_an_argument(self):
        completed = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with patch.object(privileged.subprocess, "run", return_value=completed) as run:
            ok, _ = privileged.wifi_connect("Home", "correct horse battery")
        self.assertTrue(ok)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["wifi-connect", "Home"])
        self.assertNotIn("correct horse battery", " ".join(argv))
        self.assertEqual(run.call_args.kwargs["input"], "correct horse battery\n")

    def run_helper(self, ssid, stdin):
        """The real helper, with nmcli swapped for a stub that echoes its
        arguments, so the shell logic runs without touching the network."""
        with tempfile.TemporaryDirectory() as temp:
            stub = Path(temp) / "nmcli"
            stub.write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
            stub.chmod(0o755)
            helper = Path(temp) / "pi-control.sh"
            helper.write_text(SCRIPT.read_text().replace("/usr/bin/nmcli", str(stub)))
            result = subprocess.run(
                ["sh", str(helper), "wifi-connect", ssid],
                input=stdin, capture_output=True, text=True, timeout=10,
            )
        return result.stdout.rstrip("|").split("|")

    def test_the_helper_reads_the_passphrase_from_stdin(self):
        self.assertEqual(
            self.run_helper("Home", " spaced \\ pass \n"),
            ["device", "wifi", "connect", "Home", "password", " spaced \\ pass "],
        )

    def test_an_open_network_needs_no_passphrase(self):
        self.assertEqual(self.run_helper("Cafe", ""), ["device", "wifi", "connect", "Cafe"])


class HelperLocationTests(unittest.TestCase):
    """sudo runs whatever file the rule names, so the root-owned installed
    copy must win over the checkout's whenever it exists."""

    def test_the_installed_copy_is_preferred(self):
        with tempfile.TemporaryDirectory() as temp:
            installed = Path(temp) / "solar-monitor-pi-control"
            installed.write_text("#!/bin/sh\n")
            with patch.object(privileged, "INSTALLED_HELPER", installed):
                self.assertEqual(privileged.helper_path(), installed)

    def test_the_checkout_copy_is_the_fallback(self):
        with patch.object(privileged, "INSTALLED_HELPER", Path("/nonexistent/helper")):
            self.assertEqual(privileged.helper_path(), SCRIPT)

    def test_no_helper_at_all_is_a_clear_error(self):
        with patch.object(privileged, "INSTALLED_HELPER", Path("/nonexistent/a")), \
                patch.object(privileged, "CHECKOUT_HELPER", Path("/nonexistent/b")):
            ok, message = privileged.reboot()
        self.assertFalse(ok)
        self.assertIn("/nonexistent/a", message)


class WifiScanTests(unittest.TestCase):
    def test_terse_output_is_split_on_unescaped_colons_only(self):
        networks = privileged.parse_wifi_scan("Cafe\\:5G:71:WPA2\nHome:88:WPA2 WPA3\n")
        self.assertEqual([n["ssid"] for n in networks], ["Home", "Cafe:5G"])
        self.assertEqual(networks[1]["signal_percent"], 71)

    def test_each_network_is_listed_once_at_its_strongest(self):
        output = "Home:40:WPA2\nHome:88:WPA2\n:90:WPA2\nOpenNet:55:\n"
        networks = privileged.parse_wifi_scan(output)
        self.assertEqual(
            [(n["ssid"], n["signal_percent"], n["security"]) for n in networks],
            [("Home", 88, "WPA2"), ("OpenNet", 55, "Open")],
        )


if __name__ == "__main__":
    unittest.main()
