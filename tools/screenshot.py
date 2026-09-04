#!/usr/bin/env python3
"""Capture dashboard screenshots with headless Chromium.

There is no browser on a Pi OS Lite box by default, so the front end is
otherwise only checked statically. With `chromium` installed this renders the
real pages at real viewport sizes, which is the only way to see what the
charts actually look like.

Deliberately uses Chromium's own --screenshot flag rather than a driver
library: no npm packages, nothing added to the runtime.

    python3 tools/screenshot.py --base http://127.0.0.1:5000 --out /tmp/shots
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# name -> (path, width, height)
VIEWS = {
    "dashboard-phone": ("/", 390, 844),
    "dashboard-desktop": ("/", 1280, 900),
    "estimate-phone": ("/estimate", 390, 844),
    "estimate-desktop": ("/estimate", 1280, 900),
}


def find_chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def capture(binary: str, url: str, target: Path, width: int, height: int, wait_ms: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            binary, "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={wait_ms}",
            f"--screenshot={target}", url,
        ],
        check=True, capture_output=True, timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--out", default="/tmp/solar-shots")
    parser.add_argument("--wait-ms", type=int, default=6000,
                        help="virtual time budget, so polling and charts settle")
    parser.add_argument("--only", help="capture a single named view")
    args = parser.parse_args()

    binary = find_chromium()
    if not binary:
        print("No Chromium found. Install it with:\n"
              "  sudo apt install --no-install-recommends chromium", file=sys.stderr)
        return 2

    out = Path(args.out)
    views = {args.only: VIEWS[args.only]} if args.only else VIEWS
    for name, (path, width, height) in views.items():
        target = out / f"{name}.png"
        capture(binary, f"{args.base}{path}", target, width, height, args.wait_ms)
        size = target.stat().st_size if target.exists() else 0
        print(f"  {name:20s} {width}x{height}  {target}  ({size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
