from __future__ import annotations

from pathlib import Path
import subprocess

# Every privileged action goes through this single, argument-validated
# wrapper script rather than granting the service account broad sudo rights.
# See deploy/pi-control.sh and deploy/solar-monitor-sudoers.example.
_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "pi-control.sh"
_GOVERNORS = {"powersave", "ondemand", "performance", "schedutil"}


def _run(*args: str, timeout: float = 15.0) -> tuple[bool, str]:
    if not _SCRIPT.exists():
        return False, "Privileged helper script not found (expected deploy/pi-control.sh)"
    try:
        result = subprocess.run(
            ["sudo", "-n", str(_SCRIPT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "sudo is not available on this system"
    except subprocess.TimeoutExpired:
        return False, "The command timed out"
    if result.returncode != 0:
        message = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        if "password is required" in message.lower() or "no tty present" in message.lower():
            message = (
                "Passwordless sudo isn't configured for this action yet. "
                "See deploy/solar-monitor-sudoers.example for the one-time setup."
            )
        return False, message
    return True, result.stdout.strip()


def reboot() -> tuple[bool, str]:
    return _run("reboot")


def restart_service() -> tuple[bool, str]:
    return _run("restart-service")


def set_governor(mode: str) -> tuple[bool, str]:
    if mode not in _GOVERNORS:
        return False, f"Unsupported governor mode: {mode}"
    return _run("governor", mode)


def wifi_scan() -> tuple[bool, str]:
    return _run("wifi-scan")


def wifi_connect(ssid: str, password: str) -> tuple[bool, str]:
    return _run("wifi-connect", ssid, password, timeout=30)


def parse_wifi_scan(output: str) -> list[dict]:
    networks = []
    for line in output.splitlines():
        parts = line.split(":")
        if not parts or not parts[0]:
            continue
        ssid = parts[0]
        signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        security = parts[2] if len(parts) > 2 else ""
        networks.append({"ssid": ssid, "signal_percent": signal, "security": security or "Open"})
    networks.sort(key=lambda item: item["signal_percent"] or 0, reverse=True)
    return networks
