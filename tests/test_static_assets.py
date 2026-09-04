"""Static checks on the front-end assets.

There is no browser or JS engine on the Pi, so a syntax error or a script
loaded in the wrong order fails silently in production and nowhere else.
These tests stand in for the runtime that does not exist here, in the same
spirit as the DOM-hook checks in test_templates.py.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "solar_monitor" / "static"
TEMPLATES = ROOT / "solar_monitor" / "templates"


def find_node() -> str | None:
    """Node is an optional development tool, never a runtime dependency: the
    Pi serves plain script tags. When it is present the front-end gets real
    parsing and real unit tests; when it is not, the static checks below still
    hold the line."""
    found = shutil.which("node")
    if found:
        return found
    local = Path(os.path.expanduser("~/.local/node/bin/node"))
    return str(local) if local.exists() else None

SCRIPTS = ("theme.js", "chart-core.js", "app.js", "estimate.js", "metric-picker.js", "admin.js")

# Pages and the scripts they must load in order. ChartCore is an IIFE assigned
# to a const, so a consumer loaded first sees a ReferenceError.
LOAD_ORDER = {
    "index.html": ["chart-core.js", "app.js"],
    "estimate.html": ["chart-core.js", "estimate.js"],
}

# Pages whose colour scheme theme.js decides. It must run before the page's
# own stylesheets so the first paint is already in the right scheme.
THEMED_PAGES = ("index.html", "estimate.html")

PAIRS = {"{": "}", "(": ")", "[": "]"}


def unbalanced(source: str) -> str:
    """Walk the source honouring strings, template literals, comments and
    regex literals, and report the first bracket that does not match."""
    index, length = 0, len(source)
    stack: list[tuple[str, int]] = []
    previous = ""
    while index < length:
        char = source[index]
        pair = source[index:index + 2]
        if pair == "//":
            newline = source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if pair == "/*":
            close = source.find("*/", index + 2)
            index = length if close < 0 else close + 2
            continue
        if char in "\"'":
            index += 1
            while index < length and source[index] != char:
                index += 2 if source[index] == "\\" else 1
            index += 1
            previous = "value"
            continue
        if char == "`":
            index += 1
            while index < length and source[index] != "`":
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index:index + 2] == "${":
                    depth, index = 1, index + 2
                    while index < length and depth:
                        depth += (source[index] == "{") - (source[index] == "}")
                        index += 1
                    continue
                index += 1
            index += 1
            previous = "value"
            continue
        # A slash after an operator starts a regex; after a value it divides.
        if char == "/" and previous in ("", "operator"):
            index += 1
            while index < length and source[index] not in "/\n":
                index += 2 if source[index] == "\\" else 1
            index += 1
            previous = "value"
            continue
        if char in PAIRS:
            stack.append((char, index))
            previous = "operator"
        elif char in PAIRS.values():
            if not stack:
                return f"unexpected {char!r} at offset {index}"
            opener, position = stack.pop()
            if PAIRS[opener] != char:
                return f"{opener!r} at {position} closed by {char!r} at {index}"
            previous = "value"
        elif not char.isspace():
            previous = "value" if (char.isalnum() or char in "_$") else "operator"
        index += 1
    if stack:
        opener, position = stack[-1]
        return f"unclosed {opener!r} at offset {position}"
    return ""


class StaticAssetTests(unittest.TestCase):
    def test_scripts_have_balanced_brackets(self):
        for name in SCRIPTS:
            path = STATIC / name
            self.assertTrue(path.exists(), f"{name} is missing")
            problem = unbalanced(path.read_text(encoding="utf-8"))
            self.assertEqual(problem, "", f"{name}: {problem}")

    def test_the_balance_check_can_actually_fail(self):
        self.assertNotEqual(unbalanced("function broken() { if (x) { return 1; }"), "")
        self.assertEqual(unbalanced("const url = 'https://example.com/a'; f({a: [1]});"), "")
        self.assertEqual(unbalanced("const re = /[{(]/g; const t = `x ${ {a: 1}.a } y`;"), "")

    def test_shared_chart_core_loads_before_its_consumers(self):
        for page, scripts in LOAD_ORDER.items():
            markup = (TEMPLATES / page).read_text(encoding="utf-8")
            positions = []
            for script in scripts:
                position = markup.find(f"/static/{script}")
                self.assertNotEqual(position, -1, f"{page} does not load {script}")
                positions.append(position)
            self.assertEqual(
                positions, sorted(positions),
                f"{page} loads {scripts} out of order; ChartCore must come first",
            )

    def test_theme_script_runs_in_head_before_the_page_styles(self):
        for page in THEMED_PAGES:
            markup = (TEMPLATES / page).read_text(encoding="utf-8")
            head = markup.split("</head>", 1)[0]
            script = head.find("/static/theme.js")
            self.assertNotEqual(script, -1, f"{page} does not load theme.js in <head>")
            for sheet in ("style.css", "dashboard.css", "estimate.css"):
                position = head.find(f"/static/{sheet}")
                if position != -1:
                    self.assertLess(script, position, f"{page}: theme.js must precede {sheet}")

    def test_dark_scheme_is_keyed_on_the_theme_attribute_only(self):
        # theme.js stamps data-theme on <html>; a stylesheet that also keys
        # on prefers-color-scheme would override the viewer's explicit choice.
        for sheet in STATIC.glob("*.css"):
            self.assertNotIn(
                "prefers-color-scheme", sheet.read_text(encoding="utf-8"),
                f"{sheet.name} bypasses the theme toggle with a media query",
            )

    def test_no_retired_assets_are_still_referenced(self):
        for template in TEMPLATES.glob("*.html"):
            markup = template.read_text(encoding="utf-8")
            for retired in ("lab-overrides.css", "lab-menu.js", "v2.css", "lab.html", "v2.html"):
                self.assertNotIn(retired, markup, f"{template.name} still references {retired}")


class NodeBackedTests(unittest.TestCase):
    """Stronger checks that need a JS engine. Skipped rather than failed when
    one is unavailable, so the Pi's own test run never depends on it."""

    def setUp(self):
        self.node = find_node()
        if not self.node:
            self.skipTest("no Node.js available; static checks still apply")

    def test_every_script_parses(self):
        for name in SCRIPTS:
            result = subprocess.run(
                [self.node, "--check", str(STATIC / name)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, f"{name} failed to parse:\n{result.stderr}")

    def test_chart_core_unit_tests_pass(self):
        result = subprocess.run(
            [self.node, "--test", "tests/js/*.test.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(
            result.returncode, 0,
            f"chart-core JS tests failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}",
        )


if __name__ == "__main__":
    unittest.main()
