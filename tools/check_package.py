#!/usr/bin/env python3
"""Check the installed wheel from outside the source checkout."""
from importlib.resources import files
from pathlib import Path
import tempfile

import solar_monitor
from solar_monitor.collector import Collector
from solar_monitor.config import SerialConfig
from solar_monitor.db import Database
from solar_monitor.web import create_app


def main():
    root = Path(__file__).resolve().parents[1]
    if Path(solar_monitor.__file__).resolve().is_relative_to(root):
        raise RuntimeError("This check must import the installed wheel, not the source checkout")
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / 'package.sqlite3')
        config = SerialConfig(port='sim://', require_clock_sync=False)
        collector = Collector(config, database)
        if not collector.collect_once():
            raise RuntimeError("Installed simulator did not return valid telemetry")
        client = create_app(database, config, collector).test_client()
        paths = ['/', '/estimate', '/admin/login']
        paths += ['/static/' + asset.name for asset in files('solar_monitor').joinpath('static').iterdir()
                  if asset.is_file()]
        for path in paths:
            response = client.get(path)
            if response.status_code != 200 or not response.data:
                raise RuntimeError(f"Installed asset failed: {path} (HTTP {response.status_code})")
        if not client.get('/api/status').json['sample']:
            raise RuntimeError("Installed API returned no sample")
    print('Installed wheel passed: simulator, collector, API, templates and static assets')


if __name__ == '__main__':
    main()
