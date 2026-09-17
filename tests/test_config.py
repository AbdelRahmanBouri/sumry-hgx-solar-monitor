import tempfile
import unittest
from pathlib import Path

from solar_monitor.config import AppConfig, SiteConfig, load_config


BASE = """
[serial]
port = "/dev/ttyUSB0"

[web]
port = 8080
"""

SITE = """
[site]
latitude = 34.0522
longitude = -118.2437
array_capacity_watts = 2300
performance_ratio = 0.8
tilt_degrees = 20
azimuth_degrees = 180
dual_slope = true
split_strings = true
array_description = "10 x Example 230 W modules, 5S2P"
module_count = 10
module_watts = 230
wiring = "5S2P"
vmp_volts = 150.0
voc_volts = 180.0
imp_amps = 15.0
isc_amps = 16.0
inverter_model = "Example 3 kW 24 V hybrid inverter"
charge_limit_amps = 60
battery_description = "4 x Example 100 Ah AGM, 2S2P"
battery_nominal_volts = 24
battery_capacity_ah = 200
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def load(self, text: str) -> AppConfig:
        path = Path(self.temp.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_a_site_table_loads_into_the_site_profile(self):
        config = self.load(BASE + SITE)
        self.assertEqual(config.serial.port, "/dev/ttyUSB0")
        self.assertEqual(config.web.port, 8080)
        self.assertEqual(config.site.latitude, 34.0522)
        self.assertEqual(config.site.longitude, -118.2437)
        self.assertEqual(config.site.array_capacity_watts, 2300)
        self.assertEqual(config.site.performance_ratio, 0.8)
        self.assertEqual(config.site.tilt_degrees, 20)
        self.assertEqual(config.site.azimuth_degrees, 180)
        self.assertTrue(config.site.dual_slope)
        self.assertTrue(config.site.split_strings)
        self.assertEqual(config.site.array_description, "10 x Example 230 W modules, 5S2P")
        self.assertEqual(config.site.module_count, 10)
        self.assertEqual(config.site.module_watts, 230)
        self.assertEqual(config.site.wiring, "5S2P")
        self.assertEqual(config.site.vmp_volts, 150.0)
        self.assertEqual(config.site.voc_volts, 180.0)
        self.assertEqual(config.site.imp_amps, 15.0)
        self.assertEqual(config.site.isc_amps, 16.0)
        self.assertEqual(config.site.inverter_model, "Example 3 kW 24 V hybrid inverter")
        self.assertEqual(config.site.charge_limit_amps, 60)
        self.assertEqual(config.site.battery_description, "4 x Example 100 Ah AGM, 2S2P")
        self.assertEqual(config.site.battery_nominal_volts, 24)
        self.assertEqual(config.site.battery_capacity_ah, 200)

    def test_a_config_without_a_site_table_has_no_profile(self):
        config = self.load(BASE)
        self.assertEqual(config.site, SiteConfig())
        self.assertIsNone(config.site.latitude)
        self.assertIsNone(config.site.battery_capacity_ah)
        self.assertFalse(config.site.dual_slope)
        # The other tables still load normally alongside the absent one.
        self.assertEqual(config.serial.port, "/dev/ttyUSB0")

    def test_a_partial_site_table_leaves_the_rest_unknown(self):
        config = self.load(BASE + "\n[site]\nlatitude = 34.0522\nlongitude = -118.2437\n")
        self.assertEqual(config.site.latitude, 34.0522)
        self.assertIsNone(config.site.array_capacity_watts)
        self.assertIsNone(config.site.inverter_model)

    def test_an_unknown_site_key_names_the_table_and_the_key(self):
        with self.assertRaises(ValueError) as caught:
            self.load(BASE + "\n[site]\nlatitud = 34.0522\n")
        message = str(caught.exception)
        self.assertIn("[site]", message)
        self.assertIn("'latitud'", message)
        # The message helps the user fix it rather than just refusing.
        self.assertIn("latitude", message)

    def test_an_unknown_key_in_any_table_is_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            self.load("[serial]\nbaud = 9600\n")
        self.assertIn("[serial]", str(caught.exception))
        self.assertIn("'baud'", str(caught.exception))

    def test_a_missing_file_yields_the_defaults(self):
        config = load_config(Path(self.temp.name) / "absent.toml")
        self.assertEqual(config, AppConfig())
        self.assertEqual(config.site, SiteConfig())


if __name__ == "__main__":
    unittest.main()
