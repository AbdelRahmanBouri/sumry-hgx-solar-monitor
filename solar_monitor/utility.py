"""Whether utility power is present, and whether anything is actually using it.

The inverter reports utility voltage and frequency, so "is there mains at the
input" is a direct reading. "Is the house running on it" is not: the frame
carries no trustworthy utility current or power field. Offset 76 looked like
one for two weeks and was not -- see the note in protocols/sumry_8851.py.

Two independent readings answer it anyway:

* **The inverter's own output stage.** `output_voltage * output_current` is
  what the inverter is synthesising. When the transfer relay closes and mains
  feeds the loads directly, that product collapses to near zero while
  `ac_load_watts` keeps reporting the real load. Across 2,316 grid-present
  samples this is cleanly bimodal -- either under 20% of load or over 100% of
  it -- and across 6,147 grid-absent samples it never once fell below 100%.
  So a low ratio means bypass, with no false positives in two weeks of data.

* **The power balance.** `load + battery charging - PV` is the power that has
  to be arriving from somewhere else, which is the utility's contribution in
  watts. It is the only way to size the draw, since nothing meters it.

Current follows from that power and the measured utility voltage. It is real
(in-phase) current, not a clamp-meter reading: true RMS would be higher by
1/PF, and this inverter's own output runs a power factor of 0.85-1.0. So read
it as a close lower bound on the amps coming in, which is the honest most
that can be said without a meter on the line.

Stdlib only, a pure function over one sample row, so it is testable with
synthetic readings.
"""
from __future__ import annotations

from typing import Any

# Below this the inverter is reporting no usable mains, not a brownout: a
# disconnected input reads a clean 0.0, and the lowest live reading observed
# on this installation was 118.5 V.
PRESENT_VOLTS = 80.0
# The inverter's own output as a share of the measured load. The observed gap
# runs from 0.2 to 1.0; half way across it separates bypass from inverting
# without sitting near either cluster.
BYPASS_RATIO = 0.5
# Under this much load the ratio is dominated by the inverter's own idle draw
# and says nothing about where the power is coming from.
MIN_LOAD_WATTS = 50.0


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # reject NaN


def utility_state(sample: dict[str, Any] | None) -> dict[str, Any]:
    """Classify one reading as absent / carrying the load / present but idle.

    `state` is the headline:

    * `absent`   -- no mains at the input, so the battery is the only backup.
    * `carrying` -- mains is feeding the loads through the transfer relay.
    * `standby`  -- mains is there but PV and battery are doing the work.
    * `unknown`  -- mains is there and the load is too small to attribute.

    `watts` is the utility's share from the power balance, present only when
    it can be computed, and never negative. `amps` is that power over the
    measured utility voltage -- a lower bound on the current coming in, not a
    metered reading; see the module docstring.
    """
    sample = sample or {}
    volts = _number(sample.get("grid_voltage"))
    present = volts is not None and volts >= PRESENT_VOLTS
    result: dict[str, Any] = {
        "present": present,
        "volts": volts,
        "hertz": _number(sample.get("grid_frequency")),
        "carrying_load": False if not present else None,
        "state": "absent" if not present else "unknown",
        "watts": 0.0 if not present else None,
        "amps": 0.0 if not present else None,
    }
    if not present:
        return result

    load = _number(sample.get("ac_load_watts"))
    out_volts = _number(sample.get("output_voltage"))
    out_amps = _number(sample.get("output_current"))

    if load is not None and load >= MIN_LOAD_WATTS and out_volts is not None and out_amps is not None:
        inverter_watts = out_volts * out_amps
        carrying = inverter_watts < BYPASS_RATIO * load
        result["carrying_load"] = carrying
        result["state"] = "carrying" if carrying else "standby"

    # Sizing the draw needs the whole balance; any missing term and the number
    # would be a guess, so it stays absent rather than wrong.
    pv = _number(sample.get("pv_watts"))
    batt_volts = _number(sample.get("battery_voltage"))
    batt_amps = _number(sample.get("battery_current"))
    if None not in (load, pv, batt_volts, batt_amps):
        balance = load + batt_volts * batt_amps - pv
        watts = round(max(0.0, balance), 1)
        result["watts"] = watts
        result["amps"] = round(watts / volts, 2)
    return result
