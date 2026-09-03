#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging

from solar_monitor.collector import start_collector
from solar_monitor.config import load_config
from solar_monitor.db import Database
from solar_monitor.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="SUMRY HGX read-only solar monitor")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--no-collector", action="store_true")
    parser.add_argument("--host", help="Override configured web bind address")
    parser.add_argument("--port", type=int, help="Override configured web port")
    args = parser.parse_args()
    config = load_config(args.config)
    database = Database(config.storage.database)
    collector = None
    if not args.no_collector:
        collector, _ = start_collector(config.serial, database)
    app = create_app(database, config.serial, collector)
    app.run(host=args.host or config.web.host, port=args.port or config.web.port, debug=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
