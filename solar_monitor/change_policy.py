"""Which proposed inverter changes may be applied without a person.

Three gates stand between a proposal and the wire, narrowest first:

1. **This policy.** Off by default, empty by default, and always a subset of
   what the protocol allows. It is the only thing that can release a change
   without someone clicking approve, and an admin writes it deliberately.
2. **The protocol's writable-field table**, which bounds every field to the
   inverter's own menu limits.
3. **assert_write_frame**, which refuses any frame differing from the
   inverter's own last reply outside those fields.

A proposal outside the policy is not rejected -- it simply waits for a
person. That is the point: automation runs the routine envelope, and
anything novel still gets a human.
"""
from __future__ import annotations

from typing import Any

POLICY_SETTING = "inverter_auto_apply_policy"
ENABLED_SETTING = "inverter_auto_apply"


def load_policy(settings: dict[str, str]) -> dict[str, Any]:
    import json
    if settings.get(ENABLED_SETTING) != "true":
        return {}
    try:
        policy = json.loads(settings.get(POLICY_SETTING) or "{}")
    except ValueError:
        return {}
    return policy if isinstance(policy, dict) else {}


def _allows(rule: Any, value: Any) -> bool:
    if isinstance(rule, list):
        return value in rule
    if isinstance(rule, dict):
        low, high = rule.get("min"), rule.get("max")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if low is not None and number < float(low):
            return False
        if high is not None and number > float(high):
            return False
        return True
    return False


def evaluate(changes: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str]]:
    """(may auto-apply, reasons it may not). Every field must be covered:
    one uncovered field sends the whole proposal to a person."""
    if not changes:
        return False, ["no changes proposed"]
    if not policy:
        return False, ["automatic application is switched off"]
    blocked = []
    for name, value in sorted(changes.items()):
        rule = policy.get(name)
        if rule is None:
            blocked.append(f"{name} is not in the automatic policy")
        elif not _allows(rule, value):
            blocked.append(f"{name}={value} is outside the policy's range")
    return (not blocked), blocked
