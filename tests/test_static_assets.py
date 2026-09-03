"""Static checks on the front-end assets.

There is no browser or JS engine on the Pi, so a syntax error or a script
loaded in the wrong order fails silently in production and nowhere else.
These tests stand in for the runtime that does not exist here, in the same
spirit as the DOM-hook checks in test_templates.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "solar_monitor" / "static"
TEMPLATES = ROOT / "solar_monitor" / "templates"

SCRIPTS = ("chart-core.js", "app.js", "estimate.js", "metric-picker.js", "admin.js")

# Pages and the scripts they must load in order. ChartCore is an IIFE assigned
# to a const, so a consumer loaded first sees a ReferenceError.
LOAD_ORDER = {
    "index.html": ["chart-core.js", "app.js"],
    "estimate.html": ["chart-core.js", "estimate.js"],
}

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

    def test_no_retired_assets_are_still_referenced(self):
        for template in TEMPLATES.glob("*.html"):
            markup = template.read_text(encoding="utf-8")
            for retired in ("lab-overrides.css", "lab-menu.js", "v2.css", "lab.html", "v2.html"):
                self.assertNotIn(retired, markup, f"{template.name} still references {retired}")


if __name__ == "__main__":
    unittest.main()
