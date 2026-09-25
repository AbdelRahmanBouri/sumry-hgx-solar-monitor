"""Battery state of charge and health, from the readings the inverter logs.

Terminal voltage is a poor state-of-charge gauge for a lead-acid bank under
charge or load: 40 A of charge pushes it up, a kilowatt of load pulls it
down, and neither says how much is in the bank. So charge is counted
instead -- current integrated over time against the bank's capacity -- and
the count is re-anchored whenever the bank shows its hand:

* **Full.** The charger has held the bank at its plateau and the current has
  tapered to a trickle: the bank is as full as this charger will make it.
* **Rest.** Nearly no current for long enough that the terminal voltage is
  close to open-circuit voltage, which maps to state of charge through the
  chemistry's own curve.

Every full-to-rest stretch also reveals the bank's *effective* capacity:
the charge that came out divided by the share of the bank it emptied. That
learned figure replaces the nameplate once seen, so the counter, the
runtime estimate and the health panel all describe the bank as it is
rather than as it was sold.

Stdlib only, pure functions over sample rows, so it is testable with
synthetic days.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# --- Chemistry ------------------------------------------------------------
# Odyssey (TPPL AGM) open-circuit voltage against state of charge, per 12 V,
# from the manufacturer's manual. Interpolated; doubled for a 24 V bank.
OCV_12V = ((11.58, 0.0), (11.88, 25.0), (12.18, 50.0), (12.50, 75.0), (12.84, 100.0))
CHARGE_EFFICIENCY = 0.92   # Ah stored per Ah pushed in; AGM is near the top for lead-acid.
# Terminal resistance of the bank, ohms. TPPL AGM is a few milliohms per
# battery; at the light loads a rest anchor allows this correction is worth
# only tens of millivolts, but it is the right sign and costs nothing.
INTERNAL_RESISTANCE_OHMS = 0.003

# --- Anchors ----------------------------------------------------------------
HOLDING_VOLTS = 26.9        # Above this the charger is regulating, not the panels.
FULL_TAIL_AMPS = 5.0        # Below this at the plateau the bank is taking a trickle.
FULL_HOLD_MINUTES = 10
# Sitting at float voltage is not proof of a full bank: on a fading afternoon
# the current drops below the tail threshold because there is no more sun, not
# because the bank stopped accepting. A full anchor therefore also requires a
# real absorption to have happened recently.
ABSORPTION_RECENCY_HOURS = 6
ABSORPTION_ANCHOR_BAND = 0.35

# The absorption voltage the chemistry actually wants, for a 24 V bank. A
# charger set below this stops pushing before the bank is full, so its "done"
# is short of 100% -- which makes every capacity measurement taken against it
# a lower bound, not an estimate.
RECOMMENDED_ABSORPTION_V = 28.8
SETPOINT_TOLERANCE_V = 0.1
# A true zero-current rest never happens on a lived-in system: this bank's
# quietest overnight stretch sits at 3-4 A and never spends 30 minutes below
# 1.5 A, so a fixed low threshold would mean the capacity was never measured
# at all. C/50 is light enough that terminal voltage is within millivolts of
# open circuit once polarisation has settled, and it scales with the bank.
REST_C_RATE = 1 / 50
REST_MINUTES = 30           # Long enough for polarisation to settle.
# ...and the charger must not be driving the bank. Terminal voltage while the
# charger holds float is its setpoint, not open circuit: reading the chemistry
# curve off 26.98 V at +3 A returns a confident, wrong, 100%. Any real charge
# current disqualifies a rest. A depleted bank at float would pull many amps,
# so a trickle under this threshold does mean the charger has nothing to push.
REST_MAX_CHARGE_AMPS = 1.0
MAX_GAP_HOURS = 5 / 60      # A gap longer than this in the data is not integrated across.

# --- Capacity learning -------------------------------------------------------
MIN_LEARNING_DEPTH = 0.15   # A full-to-rest stretch must empty this share to count.
LEARNING_BLEND = 0.5        # Weight of the newest measurement against the running value.

# --- Absorption detection ----------------------------------------------------
ABSORPTION_BAND = 0.3        # Within this of the bulk setpoint counts as holding it.
MIN_ABSORPTION_MINUTES = 5   # Shorter than this is a transient, not an absorption.
ABSORPTION_TAIL_SAMPLES = 5  # Readings at the end of the hold that define its tail.
DAYLIGHT_WATTS = 20


def _when(row: dict) -> datetime:
    stamp = datetime.fromisoformat(row["captured_at"])
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def open_circuit_voltage(terminal_volts: float, amps: float) -> float:
    """Terminal voltage corrected back to open circuit. Discharging pulls the
    terminals below open circuit and charging pushes them above it."""
    return terminal_volts - amps * INTERNAL_RESISTANCE_OHMS


def soc_from_rest_voltage(volts: float, cells_nominal: int = 24) -> float:
    """State of charge implied by an open-circuit voltage, 0-100."""
    per_12v = volts * 12 / cells_nominal
    if per_12v <= OCV_12V[0][0]:
        return 0.0
    if per_12v >= OCV_12V[-1][0]:
        return 100.0
    for (low_v, low_soc), (high_v, high_soc) in zip(OCV_12V, OCV_12V[1:]):
        if low_v <= per_12v <= high_v:
            return low_soc + (high_soc - low_soc) * (per_12v - low_v) / (high_v - low_v)
    return 100.0


def count_charge(
    samples: list[dict],
    nominal_ah: float | None,
    bulk_setpoint_v: float | None = None,
    recommended_absorption_v: float = RECOMMENDED_ABSORPTION_V,
) -> dict:
    """Walk the readings in order, integrating current and re-anchoring on
    full and rest events. Returns the state at the last reading.

    `nominal_ah` is the bank's nameplate. Without a positive one there is
    nothing to count against, and the state comes back "unavailable" with
    its shape intact rather than as a guess.

    `bulk_setpoint_v` is what the charger is actually set to. When it is
    below what the chemistry wants, the charger's "full" is short of full:
    the returned state says so, and the capacity figures become lower
    bounds, because a too-low anchor makes every measurement read light.
    """
    charger_limited = (
        bulk_setpoint_v is not None
        and bulk_setpoint_v < recommended_absorption_v - SETPOINT_TOLERANCE_V
    )
    state: dict[str, Any] = {
        "soc": None, "method": "unavailable", "capacity_ah": nominal_ah,
        "capacity_source": "nameplate", "capacity_measurements": [],
        "last_full_at": None, "last_rest_at": None, "last_anchor_at": None,
        "ah_in_since_full": 0.0, "ah_out_since_full": 0.0,
        "full_events": [], "rest_events": [], "sample_count": 0,
        "charger_limited": charger_limited,
        "bulk_setpoint_v": bulk_setpoint_v,
        "recommended_absorption_v": recommended_absorption_v,
        "capacity_is_lower_bound": False,
    }
    if not nominal_ah or nominal_ah <= 0:
        state["capacity_ah"] = None
        return state
    rows = [row for row in samples if _number(row.get("battery_voltage")) is not None
            and _number(row.get("battery_current")) is not None]
    rows.sort(key=_when)
    state["sample_count"] = len(rows)
    if not rows:
        return state

    soc: float | None = None
    capacity = nominal_ah
    full_run_start: datetime | None = None
    rest_run_start: datetime | None = None
    full_anchored = rest_anchored = False  # this run has already fired
    last_absorption: datetime | None = None
    # Above this the bank is genuinely in absorption rather than on float.
    absorption_floor = (
        bulk_setpoint_v - ABSORPTION_ANCHOR_BAND if bulk_setpoint_v is not None else None
    )
    # Charge taken out since the last full anchor, for capacity learning.
    out_since_full = 0.0
    in_since_full = 0.0
    full_seen = False
    previous = rows[0]

    for row in rows[1:]:
        now, before = _when(row), _when(previous)
        hours = (now - before).total_seconds() / 3600
        amps = (_number(row["battery_current"]) + _number(previous["battery_current"])) / 2
        volts = _number(row["battery_voltage"])
        previous = row
        if hours <= 0:
            continue
        if hours <= MAX_GAP_HOURS:
            ah = amps * hours
            if ah > 0:
                stored = ah * CHARGE_EFFICIENCY
                in_since_full += stored
                if soc is not None:
                    soc = min(100.0, soc + stored / capacity * 100)
            else:
                out_since_full += -ah
                if soc is not None:
                    soc = max(0.0, soc + ah / capacity * 100)
        else:
            # Data gap: the runs cannot be trusted across it.
            full_run_start = rest_run_start = None
            full_anchored = rest_anchored = False

        if absorption_floor is not None and volts >= absorption_floor:
            last_absorption = now

        # Full anchor: holding the plateau while taking a trickle, *and* an
        # absorption actually happened recently. Without that second test a
        # bank idling at float on a fading afternoon anchors at 100% purely
        # because the sun ran out -- which is what the middle of this
        # installation's three anchors was doing.
        absorbed_recently = (
            absorption_floor is None
            or (last_absorption is not None
                and (now - last_absorption) <= timedelta(hours=ABSORPTION_RECENCY_HOURS))
        )
        if absorbed_recently and volts >= HOLDING_VOLTS and 0 <= _number(row["battery_current"]) < FULL_TAIL_AMPS:
            full_run_start = full_run_start or now
            if (now - full_run_start) >= timedelta(minutes=FULL_HOLD_MINUTES):
                if not full_anchored:
                    state["full_events"].append(now.isoformat())
                    full_anchored = True
                soc = 100.0
                state["last_full_at"] = now.isoformat()
                state["last_anchor_at"] = now.isoformat()
                full_seen = True
                out_since_full = in_since_full = 0.0
        else:
            full_run_start = None
            full_anchored = False

        # Rest anchor: a light enough load, for long enough, and with the
        # charger not pushing, so terminal voltage stands in for open
        # circuit. Fires once per rest; the count carries on from there.
        amps_now = _number(row["battery_current"])
        if -capacity * REST_C_RATE < amps_now < REST_MAX_CHARGE_AMPS:
            rest_run_start = rest_run_start or now
            if (now - rest_run_start) >= timedelta(minutes=REST_MINUTES) and not rest_anchored:
                rest_anchored = True
                rested = soc_from_rest_voltage(open_circuit_voltage(volts, amps_now))
                if full_seen and out_since_full >= capacity * MIN_LEARNING_DEPTH and rested < 100:
                    net_out = out_since_full - in_since_full
                    if net_out > 0:
                        measured = net_out / (1 - rested / 100)
                        capacity = capacity * (1 - LEARNING_BLEND) + measured * LEARNING_BLEND
                        state["capacity_measurements"].append({
                            "at": now.isoformat(),
                            "measured_ah": round(measured, 1),
                            "rest_soc": round(rested, 1),
                            "lower_bound": charger_limited,
                        })
                        state["capacity_source"] = "measured"
                        state["capacity_is_lower_bound"] = charger_limited
                state["rest_events"].append(now.isoformat())
                soc = rested
                state["last_rest_at"] = now.isoformat()
                state["last_anchor_at"] = now.isoformat()
                # A rest anchor restarts the learning stretch from a known point.
                out_since_full = in_since_full = 0.0
                full_seen = full_seen or rested >= 99
        else:
            rest_run_start = None
            rest_anchored = False

    state["soc"] = None if soc is None else round(soc, 1)
    state["method"] = "coulomb" if soc is not None else "unavailable"
    state["capacity_ah"] = round(capacity, 1)
    state["ah_in_since_full"] = round(in_since_full, 1)
    state["ah_out_since_full"] = round(out_since_full, 1)
    state["as_of"] = rows[-1]["captured_at"]
    return state


def runtime_to_cutoff(
    samples: list[dict],
    state: dict,
    cutoff_volts: float,
    grid_available: bool = False,
    window_minutes: int = 20,
) -> dict | None:
    """How long until the inverter's low-voltage cutoff shuts the output down.

    Written after 2026-09-05, when a 578 W overnight load emptied a bank of
    roughly 120 Ah and the inverter shut down on fault 04. At 19:05 that
    night the dashboard showed "82%", which was true and useless: what
    mattered was that the bank had about two hours left. This is that
    number.

    It counts charge, not volts, because terminal voltage under a 24 A load
    says almost nothing about what is left. The floor is the cutoff, not
    empty: on this system the inverter stops at `cutoff_volts` and, with no
    utility connected, that is a hard stop with nothing to fall back to.
    """
    if not samples or state.get("soc") is None:
        return None
    recent = samples[-1]
    amps = _number(recent.get("battery_current"))
    volts = _number(recent.get("battery_voltage"))
    if amps is None or volts is None:
        return None

    # Average the recent draw so a single spike does not swing the estimate.
    cutoff_time = _when(recent) - timedelta(minutes=window_minutes)
    window = [s for s in samples if _when(s) >= cutoff_time
              and _number(s.get("battery_current")) is not None]
    draw = -sum(_number(s["battery_current"]) for s in window) / len(window) if window else -amps

    capacity = float(state.get("capacity_ah") or 0)
    soc = float(state["soc"])
    # Charge above the cutoff, not above empty. The cutoff maps to a state of
    # charge through the resting curve; below it the inverter is off anyway.
    floor_soc = soc_from_rest_voltage(cutoff_volts)
    usable_ah = max(0.0, capacity * (soc - floor_soc) / 100)

    result = {
        "draw_amps": round(draw, 1),
        "capacity_ah": round(capacity, 1),
        "soc": round(soc, 1),
        "cutoff_volts": cutoff_volts,
        "cutoff_soc": round(floor_soc, 1),
        "usable_ah": round(usable_ah, 1),
        "grid_available": grid_available,
        "charging": draw <= 0.5,
    }
    if draw <= 0.5:
        result["hours"] = None
        return result
    result["hours"] = round(usable_ah / draw, 2)
    result["empty_at"] = (_when(recent) + timedelta(hours=usable_ah / draw)).isoformat()
    return result


def _local_day(stamp: datetime, offset: timedelta) -> str:
    return (stamp + offset).strftime("%Y-%m-%d")


def daily_health(samples: list[dict], bulk_setpoint_v: float, utc_offset_minutes: int = 0) -> list[dict]:
    """Per local day: pre-dawn voltage, absorption held and its tail
    current, and charge in and out. These are the recovery gauges: a bank
    that is being brought back rests higher each morning and finishes
    absorption at a smaller current."""
    offset = timedelta(minutes=utc_offset_minutes)
    rows = [row for row in samples if _number(row.get("battery_voltage")) is not None]
    rows.sort(key=_when)
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(_local_day(_when(row), offset), []).append(row)

    days = []
    for day, day_rows in sorted(by_day.items()):
        # Pre-dawn: the hour before the panels first make power.
        dawn_index = next(
            (i for i, row in enumerate(day_rows) if (_number(row.get("pv_watts")) or 0) > DAYLIGHT_WATTS), None
        )
        predawn = None
        predawn_amps = None
        if dawn_index:
            dawn = _when(day_rows[dawn_index])
            window = [row for row in day_rows[:dawn_index] if dawn - _when(row) <= timedelta(hours=1)]
            if len(window) >= 3:
                volts = sorted(_number(row["battery_voltage"]) for row in window)
                predawn = round(volts[len(volts) // 2], 2)
                amps = [_number(row.get("battery_current")) for row in window]
                amps = [a for a in amps if a is not None]
                predawn_amps = round(sum(amps) / len(amps), 1) if amps else None

        # Absorption is the *longest* stretch spent holding the setpoint, not
        # the first touch of it: the bank crosses the band briefly on the way
        # up, and taking that transient reported minutes and a bulk-current
        # tail for a hold that actually ran an hour and tapered to 4 A.
        runs: list[list[dict]] = []
        current_run: list[dict] = []
        for row in day_rows:
            if _number(row["battery_voltage"]) >= bulk_setpoint_v - ABSORPTION_BAND:
                current_run.append(row)
            elif current_run:
                runs.append(current_run)
                current_run = []
        open_ended = bool(current_run)  # still holding at the end of the data
        if current_run:
            runs.append(current_run)

        def run_minutes(run):
            return (_when(run[-1]) - _when(run[0])).total_seconds() / 60

        longest = max(runs, key=run_minutes, default=None)
        absorption_minutes = None
        absorption_reached = False
        absorption_ended = False
        tail_amps = None
        if longest is not None and run_minutes(longest) >= MIN_ABSORPTION_MINUTES:
            absorption_reached = True
            absorption_minutes = round(run_minutes(longest))
            absorption_ended = not (open_ended and longest is runs[-1])
            # The tail is the current the hold ended at, taken over the last
            # few readings so one noisy sample cannot define it.
            tail = [
                _number(row.get("battery_current"))
                for row in longest[-ABSORPTION_TAIL_SAMPLES:]
            ]
            tail = sorted(amps for amps in tail if amps is not None)
            if tail:
                tail_amps = tail[len(tail) // 2]

        ah_in = ah_out = 0.0
        for before, row in zip(day_rows, day_rows[1:]):
            hours = (_when(row) - _when(before)).total_seconds() / 3600
            if not 0 < hours <= MAX_GAP_HOURS:
                continue
            a, b = _number(before.get("battery_current")), _number(row.get("battery_current"))
            if a is None or b is None:
                continue
            ah = (a + b) / 2 * hours
            if ah > 0:
                ah_in += ah
            else:
                ah_out -= ah

        days.append({
            "day": day,
            "predawn_volts": predawn,
            "predawn_amps": predawn_amps,
            "absorption_minutes": absorption_minutes,
            "absorption_reached": absorption_reached,
            "absorption_ended": absorption_ended,
            "tail_amps": None if tail_amps is None else round(tail_amps, 1),
            "ah_in": round(ah_in, 1),
            "ah_out": round(ah_out, 1),
            "samples": len(day_rows),
        })
    return days
