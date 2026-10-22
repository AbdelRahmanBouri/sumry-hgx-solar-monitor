#!/usr/bin/env python3
"""Exercise the running demo's public API, login and gated simulated write.

Start run.py --demo first. Refuses a collector that does not use sim://.
"""
import argparse
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


def require(condition, message="Demo check failed"):
    if not condition:
        raise AssertionError(message)


def check_write(call):
    original = call("/api/admin/inverter")["current"]["settings"]["charge_cutoff_current_a"]
    target = 5 if original != 5 else 6
    call("/api/admin/inverter/writes", {"enabled": True})
    try:
        result = call("/api/admin/inverter/settings", {"changes": {"charge_cutoff_current_a": target}})
        require(result["verified"] and result["after"]["charge_cutoff_current_a"] == target,
                "Simulated change was not verified")
    finally:
        try:
            result = call("/api/admin/inverter/settings", {"changes": {"charge_cutoff_current_a": original}})
            require(result["verified"] and result["after"]["charge_cutoff_current_a"] == original,
                    "Original simulated value was not restored")
        finally:
            call("/api/admin/inverter/writes", {"enabled": False})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path, body=None, expected=200):
        request = urllib.request.Request(base + path)
        if body is not None:
            request.data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        try:
            response = client.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            if response.status != expected:
                raise AssertionError(f"{path}: HTTP {response.status}, expected {expected}")
            return json.load(response)

    status = call("/api/status")
    if status["collector"]["configured_port"] != "sim://":
        raise RuntimeError("This check only runs against a simulated inverter")
    require(status["sample"] and status["read_only"])
    require(call("/health")["ok"])
    require(call("/api/history?hours=24&points=10000"))
    require(call("/api/history/daily?days=7"))
    require(call("/api/battery")["state"]["soc"] is not None)
    require(call("/api/estimate")["configured"])
    call("/api/admin/inverter", expected=401)
    form = urllib.parse.urlencode({"username": "demo", "password": "demo-password"}).encode()
    with client.open(base + "/admin/login", data=form, timeout=30) as response:
        require(response.status == 200)
    require(any(cookie.name == "session" for cookie in jar))
    call("/api/admin/system/reboot", {}, expected=403)
    call("/api/admin/inverter/settings", {"changes": {"charge_cutoff_current_a": 5}}, expected=403)
    check_write(call)
    require(call("/api/status")["read_only"])
    print("Demo passed: telemetry, history, estimates, authentication, write refusal, verified write and restore")


if __name__ == "__main__":
    main()
