from __future__ import annotations

from pathlib import Path
import socket
import subprocess


def _run(args: list[str], timeout: float = 3.0) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def uptime_seconds() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def cpu_temperature_c() -> float | None:
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            return round(int(Path(path).read_text().strip()) / 1000, 1)
        except (OSError, ValueError):
            continue
    output = _run(["vcgencmd", "measure_temp"])
    if output and "=" in output:
        try:
            return float(output.split("=")[1].split("'")[0])
        except ValueError:
            return None
    return None


def cpu_load_percent() -> float | None:
    try:
        import os
        load_1min = os.getloadavg()[0]
        cpu_count = os.cpu_count() or 1
        return round(min(100.0, load_1min / cpu_count * 100), 1)
    except (OSError, AttributeError):
        return None


def memory_info() -> dict | None:
    try:
        raw = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    values = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.strip().split()[0])  # kB
    if "MemTotal" not in values:
        return None
    total_kb = values["MemTotal"]
    available_kb = values.get("MemAvailable", 0)
    used_kb = max(0, total_kb - available_kb)
    return {
        "total_mb": round(total_kb / 1024),
        "used_mb": round(used_kb / 1024),
        "percent": round(used_kb / total_kb * 100, 1) if total_kb else None,
    }


# Bit meanings from `vcgencmd get_throttled`: bits 0-3 are current state,
# bits 16-19 are "has happened since boot". Only the four documented
# conditions are surfaced; unknown bits are ignored.
_THROTTLE_BITS = {
    0: "under_voltage",
    1: "frequency_capped",
    2: "throttled",
    3: "soft_temperature_limit",
}


def throttled_flags() -> dict | None:
    output = _run(["vcgencmd", "get_throttled"])
    if not output or "=" not in output:
        return None
    try:
        mask = int(output.split("=")[1], 16)
    except ValueError:
        return None
    now = {name for bit, name in _THROTTLE_BITS.items() if mask & (1 << bit)}
    ever = {name for bit, name in _THROTTLE_BITS.items() if mask & (1 << (bit + 16))}
    return {"now": sorted(now), "since_boot": sorted(ever)}


def governor() -> str | None:
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        return None


def local_ip_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _wifi_interface() -> str | None:
    output = _run(["iw", "dev"])
    if not output:
        return None
    interface = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Interface"):
            interface = line.split()[-1]
        if interface and "type managed" in line:
            return interface
    return interface


def wifi_status() -> dict | None:
    interface = _wifi_interface()
    if not interface:
        return None
    output = _run(["iw", "dev", interface, "link"])
    if not output or output.startswith("Not connected"):
        return {"interface": interface, "connected": False}
    ssid = None
    signal_dbm = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
        elif line.startswith("signal:"):
            try:
                signal_dbm = int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    signal_percent = None
    if signal_dbm is not None:
        signal_percent = max(0, min(100, 2 * (signal_dbm + 100)))
    return {
        "interface": interface, "connected": True, "ssid": ssid,
        "signal_dbm": signal_dbm, "signal_percent": signal_percent,
    }


def network_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "ip_address": local_ip_address(),
        "wifi": wifi_status(),
    }


def service_active(name: str) -> bool | None:
    output = _run(["systemctl", "is-active", name])
    if output is None:
        return None
    return output.strip() == "active"


def public_snapshot(include_network: bool = False) -> dict:
    """System health for the unauthenticated dashboard.

    Network identity is left out unless asked for: this payload is served
    without a login, and hostname, address and SSID identify the machine
    rather than describe its health.
    """
    current_governor = governor()
    snapshot = {
        "uptime_seconds": uptime_seconds(),
        "cpu_temperature_c": cpu_temperature_c(),
        "throttled": throttled_flags(),
        "eco_mode": current_governor == "powersave" if current_governor else None,
    }
    if include_network:
        snapshot["network"] = network_info()
    return snapshot


def admin_snapshot(service_name: str = "solar-monitor") -> dict:
    return {
        **public_snapshot(include_network=True),
        "cpu_load_percent": cpu_load_percent(),
        "memory": memory_info(),
        "governor": governor(),
        "service_active": service_active(service_name),
    }


def check_updates(repo_root: Path) -> dict:
    result: dict = {"git": None, "apt": None}
    if (repo_root / ".git").exists():
        # -C, because the process's working directory is wherever the
        # service was started from, which need not be the checkout.
        git = ["git", "-C", str(repo_root)]
        fetch_output = _run([*git, "fetch", "--quiet"], timeout=20)
        if fetch_output is None:
            result["git"] = {"error": "Could not reach the git remote"}
        else:
            counts = _run([*git, "rev-list", "--left-right", "--count", "HEAD...@{u}"])
            if counts:
                try:
                    ahead, behind = (int(value) for value in counts.split())
                    result["git"] = {"ahead": ahead, "behind": behind}
                except ValueError:
                    result["git"] = {"error": "Could not parse git status"}
            else:
                result["git"] = {"error": "No upstream branch configured"}
    upgradable = _run(["apt", "list", "--upgradable"], timeout=10)
    if upgradable is not None:
        packages = [line for line in upgradable.splitlines() if line and not line.startswith("Listing...")]
        result["apt"] = {"upgradable_count": len(packages), "packages": packages[:25]}
    return result
