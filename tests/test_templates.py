"""Every dashboard template must supply the DOM hooks app.js queries.

app.js resolves its elements at load time (`const x = document.querySelector('#x')`)
and immediately attaches listeners to several of them. A template that is missing
one hook therefore throws a TypeError and the whole dashboard silently stops
updating. There is no browser or JS engine on the Pi to catch that at runtime,
so this test enforces it statically instead.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "solar_monitor" / "static"
APP_JS = STATIC / "app.js"
TEMPLATES = ROOT / "solar_monitor" / "templates"
DASHBOARDS = ("index.html",)

# Pages that ship their own script instead of the shared app.js. Each one is
# checked against its own file, because a page-specific script is exactly as
# fragile: estimate.js resolves its elements at load time too.
STANDALONE_PAGES = (("estimate.html", "estimate.js"),)


def required_ids(js_path: Path = APP_JS) -> set[str]:
    js = js_path.read_text(encoding="utf-8")
    # app.js uses document.querySelector; the standalone pages wrap it as $().
    selectors = re.findall(r"(?:querySelector(?:All)?|\$)\(\s*['\"`]([^'\"`]+)['\"`]", js)
    return {s[1:] for s in selectors if s.startswith("#") and " " not in s}


class TemplateHookTests(unittest.TestCase):
    def test_every_dashboard_supplies_every_hook_app_js_queries(self):
        ids = required_ids()
        self.assertGreater(len(ids), 40, "selector scrape looks wrong")
        for name in DASHBOARDS:
            markup = (TEMPLATES / name).read_text(encoding="utf-8")
            present = set(re.findall(r'id="([^"]+)"', markup))
            missing = sorted(ids - present)
            self.assertEqual(missing, [], f"{name} is missing hooks app.js needs: {missing}")

    def test_standalone_pages_supply_their_own_script_hooks(self):
        for page, script in STANDALONE_PAGES:
            ids = required_ids(STATIC / script)
            self.assertGreater(len(ids), 20, f"selector scrape of {script} looks wrong")
            markup = (TEMPLATES / page).read_text(encoding="utf-8")
            present = set(re.findall(r'id="([^"]+)"', markup))
            missing = sorted(ids - present)
            self.assertEqual(missing, [], f"{page} is missing hooks {script} needs: {missing}")

    def test_every_dashboard_supplies_the_collection_hooks(self):
        # Queried with querySelectorAll / attribute selectors rather than by id.
        for name in DASHBOARDS:
            markup = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("data-metric=", markup, f"{name} has no data-metric bindings")
            self.assertIn("legend-toggle", markup, f"{name} has no series toggles")
            self.assertIn("range-control", markup, f"{name} has no range control")
            self.assertIn("bottom-nav", markup, f"{name} has no bottom nav")
            for card in ("load", "output", "temperature", "cooling", "grid"):
                self.assertIn(
                    f'data-card-id="{card}"', markup,
                    f"{name} is missing the {card} card the customize dialog manages",
                )

    def test_dashboards_load_their_stylesheet_and_script(self):
        for name in DASHBOARDS:
            markup = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("app.js", markup, f"{name} does not load app.js")
            self.assertRegex(markup, r'rel="stylesheet"', f"{name} loads no stylesheet")


if __name__ == "__main__":
    unittest.main()
