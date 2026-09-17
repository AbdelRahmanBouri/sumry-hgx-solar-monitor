#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging

import waitress

from solar_monitor.collector import start_collector
from solar_monitor.config import load_config
from solar_monitor.db import Database
from solar_monitor.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="SUMRY HGX solar monitor")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--no-collector", action="store_true")
    parser.add_argument("--host", help="Override configured web bind address")
    parser.add_argument("--port", type=int, help="Override configured web port")
    parser.add_argument("--dev", action="store_true",
                        help="Serve with Flask's development server (reloader off) instead of waitress")
    args = parser.parse_args()
    config = load_config(args.config)
    database = Database(config.storage.database)
    collector = None
    if not args.no_collector:
        collector, _ = start_collector(config.serial, database)
    app = create_app(
        database, config.serial, collector,
        site=config.site, expose_network_info=config.web.expose_network_info,
    )
    host = args.host or config.web.host
    port = args.port or config.web.port
    if args.dev:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    else:
        waitress.serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
