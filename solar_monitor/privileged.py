from __future__ import annotations

from pathlib import Path
import subprocess

# Every privileged action goes through this single, argument-validated
# wrapper script rather than granting the service account broad sudo rights.
# See deploy/pi-control.sh and deploy/solar-monitor-sudoers.example.
_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "pi-control.sh"
_GOVERNORS = {"powersave", "ondemand", "performance", "schedutil"}


def _run(*args: str, timeout: float = 15.0, stdin: str | None = None) -> tuple[bool, str]:
    if not _SCRIPT.exists():
        return False, "Privileged helper script not found (expected deploy/pi-control.sh)"
    try:
        result = subprocess.run(
            ["sudo", "-n", str(_SCRIPT), *args],
            input=stdin, capture_output=True, text=True, timeout=timeout,
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
    # The passphrase travels on standard input. As an argument it would be
    # written to the system journal: sudo logs the full command line of
    # everything it runs, and /proc shows it to every local user meanwhile.
    return _run("wifi-connect", ssid, timeout=30, stdin=f"{password}\n")


def _terse_fields(line: str) -> list[str]:
    """Split one line of `nmcli -t` output. Colons inside a value arrive
    escaped as `\\:`, so an SSID like "Cafe:5G" stays one field."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def parse_wifi_scan(output: str) -> list[dict]:
    """Networks from `nmcli -t -f ssid,signal,security`, strongest first.

    nmcli lists every access point, so a network with two radios appears
    twice; it is listed once here, at its strongest signal. Hidden networks
    have no name to join by and are left out.
    """
    strongest: dict[str, dict] = {}
    for line in output.splitlines():
        parts = _terse_fields(line)
        ssid = parts[0]
        if not ssid:
            continue
        signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        security = parts[2] if len(parts) > 2 else ""
        network = {"ssid": ssid, "signal_percent": signal, "security": security or "Open"}
        known = strongest.get(ssid)
        if known is None or (signal or 0) > (known["signal_percent"] or 0):
            strongest[ssid] = network
    return sorted(strongest.values(), key=lambda item: item["signal_percent"] or 0, reverse=True)
