#!/usr/bin/env python3
"""Render the pages with headless Chromium, for review and for the README.

There is no browser on a Pi OS Lite box by default, so the front end is
otherwise only checked statically. With `chromium` installed this renders
the real pages at real viewport sizes, which is the only way to see what
the charts actually look like.

It drives Chromium over the DevTools protocol on a pipe -- the standard
library only, no npm packages and no driver library -- which is what
allows full-length pages, a dark scheme, a time zone, and pages behind the
admin login. Uncaught page errors are printed, so a broken script shows up
here rather than only in someone's browser.

    python run.py --demo &                    # or any running instance
    python tools/screenshot.py --out docs/screenshots
    python tools/screenshot.py --only admin-inverter --login demo:demo-password
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# name -> (path, width, height, scheme, full page, JavaScript to run first)
VIEWS = {
    "dashboard-phone": ("/", 390, 844, "light", False, None),
    "dashboard-desktop-dark": ("/", 1280, 900, "dark", False, None),
    "dashboard-week": ("/", 1280, 900, "light", False,
                       "document.querySelector('[data-hours=\"168\"]').click();"
                       "document.querySelector('#trends').scrollIntoView()"),
    "estimate-desktop": ("/estimate", 1280, 900, "light", False, None),
    "estimate-phone": ("/estimate", 390, 844, "light", True, None),
    "admin-inverter": ("/admin", 1280, 900, "dark", True,
                       "document.querySelector('[data-view=\"inverter\"]').click()"),
}
DEFAULT_VIEWS = ("dashboard-phone", "dashboard-desktop-dark", "estimate-desktop")


def find_chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    mac_chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if mac_chrome.is_file():
        return str(mac_chrome)
    return None


class Browser:
    """One headless Chromium, spoken to over --remote-debugging-pipe."""

    def __init__(self, binary: str):
        self.profile = tempfile.TemporaryDirectory(prefix="solar-browser-")
        read_parent, write_child = os.pipe()
        read_child, write_parent = os.pipe()
        flags = ("--headless=new --remote-debugging-pipe --no-sandbox --disable-gpu --hide-scrollbars "
                 "--no-first-run --mute-audio --disable-extensions about:blank")
        # The pipe protocol wants Chromium to read on fd 3 and write on fd 4.
        self.process = subprocess.Popen(
            ["sh", "-c", f'exec "$@" 3<&{read_child} 4>&{write_child}', "chromium", binary,
             f"--user-data-dir={self.profile.name}", *flags.split()],
            pass_fds=(read_child, write_child),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(read_child)
        os.close(write_child)
        self.writer = os.fdopen(write_parent, "wb", buffering=0)
        self.reader = os.fdopen(read_parent, "rb", buffering=0)
        self.buffer = b""
        self.last_id = 0
        self.events: list[dict] = []

    def _message(self, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while b"\0" not in self.buffer:
            ready, _, _ = select.select([self.reader], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise TimeoutError("Chromium did not reply")
            chunk = self.reader.read(65536)
            if not chunk:
                raise RuntimeError("Chromium closed the pipe")
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\0", 1)
        return json.loads(raw)

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 60) -> dict:
        self.last_id += 1
        message = {"id": self.last_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.writer.write(json.dumps(message).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self._message(max(0, deadline - time.monotonic()))
            if reply.get("id") == self.last_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            self.events.append(reply)
        raise TimeoutError(method)

    def wait_for(self, event: str, timeout: float = 60) -> None:
        if any(item.get("method") == event for item in self.events):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._message(max(0, deadline - time.monotonic()))
            self.events.append(message)
            if message.get("method") == event:
                return
        raise TimeoutError(event)

    def close(self) -> None:
        try:
            self.send("Browser.close", timeout=5)
        except (RuntimeError, TimeoutError, OSError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self.reader.close()
            self.writer.close()
            self.profile.cleanup()


def session_cookie(base: str, credentials: str) -> str:
    username, _, password = credentials.partition(":")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    form = urllib.parse.urlencode({"username": username, "password": password}).encode()
    opener.open(f"{base}/admin/login", data=form, timeout=30).read()
    for cookie in jar:
        if cookie.name == "session":
            return cookie.value
    raise RuntimeError("login failed")


def capture(browser: Browser, url: str, target: Path, width: int, height: int, *, scheme: str,
            full: bool, before: str | None, wait_ms: int, scale: float, timezone: str | None,
            cookie: str | None) -> list[str]:
    """Render one page to `target`; returns the page's uncaught errors."""
    browser.events.clear()
    target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = browser.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
    phone = width < 700
    browser.send("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": phone}, session)
    if phone:
        browser.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session)
    browser.send("Emulation.setEmulatedMedia",
                 {"features": [{"name": "prefers-color-scheme", "value": scheme}]}, session)
    if timezone:
        browser.send("Emulation.setTimezoneOverride", {"timezoneId": timezone}, session)
    if cookie:
        host = urllib.parse.urlsplit(url).hostname
        browser.send("Network.enable", {}, session)
        browser.send("Network.setCookie",
                     {"name": "session", "value": cookie, "domain": host, "path": "/", "httpOnly": True}, session)
    browser.send("Runtime.enable", {}, session)
    browser.send("Page.enable", {}, session)
    browser.send("Page.navigate", {"url": url}, session)
    browser.wait_for("Page.loadEventFired")
    if before:
        browser.send("Runtime.evaluate", {"expression": before, "awaitPromise": True}, session)
    time.sleep(wait_ms / 1000)
    params: dict = {"format": "png"}
    if full:
        metrics = browser.send("Page.getLayoutMetrics", {}, session)
        size = metrics.get("cssContentSize") or metrics["contentSize"]
        params.update(captureBeyondViewport=True,
                      clip={"x": 0, "y": 0, "width": width, "height": size["height"], "scale": 1})
    data = browser.send("Page.captureScreenshot", params, session, timeout=120)["data"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(data))
    browser.send("Target.closeTarget", {"targetId": target_id})
    return [
        event["params"]["exceptionDetails"].get("exception", {}).get("description", "uncaught error")
        for event in browser.events if event.get("method") == "Runtime.exceptionThrown"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--only", action="append", choices=sorted(VIEWS), help="a view to capture; repeatable")
    parser.add_argument("--all", action="store_true", help="capture every view, not just the README's")
    parser.add_argument("--wait-ms", type=int, default=5000, help="time for polling and charts to settle")
    parser.add_argument("--scale", type=float, default=2, help="device pixel ratio for phone views")
    parser.add_argument("--timezone", help="render as if the browser were in this IANA zone")
    parser.add_argument("--login", metavar="USER:PASSWORD", help="sign in first, for admin views")
    args = parser.parse_args()

    binary = find_chromium()
    if not binary:
        print("No Chromium found. Install it with:\n"
              "  sudo apt install --no-install-recommends chromium", file=sys.stderr)
        return 2
    names = args.only or (sorted(VIEWS) if args.all else DEFAULT_VIEWS)
    cookie = session_cookie(args.base, args.login) if args.login else None
    browser = Browser(binary)
    failures = 0
    try:
        for name in names:
            path, width, height, scheme, full, before = VIEWS[name]
            target = Path(args.out) / f"{name}.png"
            errors = capture(
                browser, f"{args.base}{path}", target, width, height, scheme=scheme, full=full,
                before=before, wait_ms=args.wait_ms, scale=args.scale if width < 700 else 1,
                timezone=args.timezone, cookie=cookie,
            )
            print(f"  {name:24s} {width}x{height} {scheme:5s}  {target}  ({target.stat().st_size // 1024} KB)")
            for error in errors:
                failures += 1
                print(f"    page error: {error.splitlines()[0]}", file=sys.stderr)
    finally:
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
