"""Propose an inverter setting change from the command line.

This queues an intent; it never touches the serial port. The running
collector is the only thing that writes to the inverter, which is what
keeps two processes off one serial line. A proposal is released either by
an admin approving it in the portal, or automatically when it falls inside
the auto-apply policy -- which is off and empty until an admin sets it.

    python -m solar_monitor.propose --set bulk_charge_voltage_v=28.4 \\
        --reason "isolate whether bulk has a ceiling below 29.2"
    python -m solar_monitor.propose --list
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import getpass
import json
import sys

from . import change_policy
from .config import DEFAULT_PATH, load_config
from .db import Database
from .protocols import create_protocol

PROPOSAL_TTL_HOURS = 12


def _now() -> str:
    return datetime.now(UTC).isoformat()


def parse_value(raw: str):
    lowered = raw.strip().lower()
    if lowered in ("true", "on", "yes"):
        return True
    if lowered in ("false", "off", "no"):
        return False
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() and "." not in raw else number


def propose(
    database: Database, changes: dict, reason: str, requested_by: str, protocol_name: str = "sumry_8851",
) -> dict:
    """Validate against the protocol, then queue. Raises ValueError if the
    change could never be written, so bad input fails here rather than
    silently waiting for an approval that would fail later."""
    settings = database.get_settings()
    protocol = create_protocol(protocol_name)
    if not hasattr(protocol, "validate_config_changes"):
        raise ValueError(f"Protocol {protocol.name} does not support writes")
    protocol.validate_config_changes(changes)

    database.expire_change_requests((datetime.now(UTC) - timedelta(hours=PROPOSAL_TTL_HOURS)).isoformat())
    automatic, blocked = change_policy.evaluate(changes, change_policy.load_policy(settings))
    if settings.get("writes_enabled") != "true":
        automatic = False
        blocked.append("inverter writes are switched off")
    request_id = database.create_change_request(
        _now(), requested_by, reason, changes,
        status="approved" if automatic else "pending",
        decided_by="auto-apply policy" if automatic else None,
        decided_at=_now() if automatic else None,
    )
    database.audit(_now(), requested_by, "inverter.change.proposed",
                   json.dumps({"id": request_id, "changes": changes, "auto": automatic}, sort_keys=True))
    return {
        "id": request_id,
        "status": "approved" if automatic else "pending",
        "automatic": automatic,
        "waiting_because": blocked,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help=f"config file (default: {DEFAULT_PATH}, if present)")
    parser.add_argument("--set", dest="assignments", action="append", default=[],
                        metavar="FIELD=VALUE", help="a setting to change; repeatable")
    parser.add_argument("--reason", default="", help="why, recorded in the audit log")
    parser.add_argument("--by", default=getpass.getuser(), help="who is proposing")
    parser.add_argument("--list", action="store_true", help="show recent requests and exit")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config or DEFAULT_PATH, required=args.config is not None)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    database = Database(config.storage.database)

    if args.list:
        for request in database.change_requests(15):
            result = request["result"] or {}
            verdict = "" if not result else ("  verified" if result.get("verified") else f"  {result.get('error', 'not verified')}")
            print(f"#{request['id']:<4} {request['status']:<10} {request['created_at'][:19]}  "
                  f"{request['changes']}{verdict}")
        return 0

    if not args.assignments:
        parser.error("nothing to propose: pass --set FIELD=VALUE")
    changes = {}
    for assignment in args.assignments:
        if "=" not in assignment:
            parser.error(f"expected FIELD=VALUE, got {assignment!r}")
        field, _, raw = assignment.partition("=")
        changes[field.strip()] = parse_value(raw)

    if not args.reason:
        parser.error("--reason is required: the audit log should say why")
    try:
        outcome = propose(database, changes, args.reason, args.by, config.serial.protocol)
    except ValueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    if outcome["automatic"]:
        print(f"Request #{outcome['id']} approved by policy; the collector will apply it on its next cycle.")
    else:
        print(f"Request #{outcome['id']} is waiting for approval in Admin -> Inverter.")
        for reason in outcome["waiting_because"]:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
