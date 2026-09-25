"""A simulated SUMRY HGX, for running the monitor without the hardware.

`python run.py --demo` serves the whole application against this model
instead of a serial adapter. The collector reaches it through an ordinary
pyserial port named `sim://` (see serial_handlers/protocol_sim.py), so the
code being demonstrated is the code that runs against a real inverter: the
same allow-listed queries go out, and CRC-checked frames in the inverter's
own layout come back, encoded with the decoder's own field tables.

The model is small but physical rather than a recording:

* Sunlight comes from the estimator's sun-position and clear-sky functions
  for the demo site, dimmed by weather seeded per calendar day -- every day
  differs, and the same day always looks the same.
* The household draws a base load, a fridge that cycles, lights in the
  evening and a few short heavy appliances.
* The bank is 24 V lead-acid with an open-circuit voltage curve, charge
  polarisation, and a three-stage charger (bulk, absorption, float) that
  follows the configuration block. When the bank is full the charger stops
  taking power the panels could have made, which is the curtailment the
  solar potential page exists to find.
* Its real capacity is below the nameplate, as an old bank's is, so the
  charge counter has something to discover.
* Configuration writes land: a write that passes the protocol's own gate
  becomes the block the next read returns, and the charger follows it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
from pathlib import Path
import random
import struct
import threading

from werkzeug.security import generate_password_hash

from . import solar_estimate
from .battery import OCV_12V
from .config import AppConfig, SerialConfig, SiteConfig, StorageConfig, WebConfig
from .db import Database
from .protocols.sumry_8851 import (
    STATUS_FIELDS,
    Sumry8851Protocol,
    _crc16_modbus,
)

# A real status reply captured from the HGX on 2026-09-02. Bytes the decoder
# does not read keep the inverter's own values; the fields it does read are
# overwritten on every frame.
STATUS_TEMPLATE = bytes.fromhex(
    "88 51 00 03 00 00 94 00 85 33 C1 0B 00 00 00 00 34 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 10 00 BC 34 00 00 00 00 00 00 00 00 00 00 00 00 AF 04 D8 01 64 17 35 02 1B 02"
    " ED 01 F3 01 0E 0D 0C 0C C2 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8A 0A 0E 00"
    " 00 00 00 00 27 09 03 01 17 02 0C 0B 00 00 16 00 00 00 1E 1E 1E 25 1E 19 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 6E 37"
)
# A real configuration reply captured on 2026-09-04, which the demo then
# configures through the protocol's own write path.
CONFIG_TEMPLATE = bytes.fromhex(
    "88 51 00 03 02 00 5E 00 80 A4 AF C4 A0 0F B0 04 70 17 00 00 00 00 48 0D A0 0F 48 0D 78 05"
    " 8C 0A B6 03 1C 0C B8 0B 34 08 98 08 78 05 A0 0F 74 0E 04 0B F0 0A 8C 0A 60 09 C8 00 20 03"
    " 32 00 00 00 00 00 3C FB 32 00 3C EC 32 F6 AC 0D AC 0D F4 01 E4 0C 00 00 55 4B 50 50 46 50"
    " 68 0B 3C 00 78 00 1E 00 32 5F 14 00 5C 39"
)
DEMO_SETTINGS = {
    "bulk_charge_voltage_v": 28.8,
    "float_charge_voltage_v": 27.2,
    "maximum_charge_current_a": 80,
    "charge_cutoff_current_a": 4,
    "low_battery_cutoff_v": 22.5,
}

STEP = timedelta(seconds=30)
INVERTER_EFFICIENCY = 0.92
INVERTER_IDLE_WATTS = 20.0
CHARGE_EFFICIENCY = 0.92
RESISTANCE_OHMS = 0.008  # the bank and its cables


def _local_meridian() -> float:
    """The longitude whose solar noon falls near noon on this machine's
    clock, so the demo's sun rises and sets when the viewer's does."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return max(-179.0, min(179.0, round(offset.total_seconds() / 3600 * 15, 2)))


def demo_site() -> SiteConfig:
    """A made-up installation. Latitude 35 N; longitude on the viewer's own
    time-zone meridian."""
    return SiteConfig(
        latitude=35.0, longitude=_local_meridian(),
        array_capacity_watts=2400, performance_ratio=0.8,
        tilt_degrees=20, azimuth_degrees=180,
        array_description="8 x 300 W modules (simulated)", module_count=8, module_watts=300,
        wiring="4S2P", vmp_volts=131.2, voc_volts=158.4, imp_amps=18.3, isc_amps=19.4,
        inverter_model="SUMRY HGX 4 kW 24 V (simulated)", charge_limit_amps=80,
        battery_description="4 x 100 Ah AGM, 2S2P (simulated)",
        battery_nominal_volts=24, battery_capacity_ah=200,
    )


def _unit_noise(seconds: float, salt: int) -> float:
    """Deterministic noise in [-0.5, 0.5) for one 30-second slot, so a slot
    reads the same whether it is backfilled or polled live."""
    slot = int(seconds // 30)
    mixed = (slot * 2654435761 + salt * 40503) & 0xFFFFFFFF
    mixed ^= mixed >> 15
    mixed = (mixed * 2246822519) & 0xFFFFFFFF
    mixed ^= mixed >> 13
    return mixed / 2**32 - 0.5


def _ocv(soc: float) -> float:
    """Open-circuit voltage of the 24 V bank at a state of charge, 0-100:
    the inverse of the charge counter's own rest curve."""
    points = [(percent, 2 * volts) for volts, percent in OCV_12V]
    if soc <= points[0][0]:
        return points[0][1]
    for (low_soc, low_v), (high_soc, high_v) in zip(points, points[1:]):
        if soc <= high_soc:
            return low_v + (high_v - low_v) * (soc - low_soc) / (high_soc - low_soc)
    return points[-1][1]


@dataclass
class _Day:
    """One day's weather and household plan, drawn from a seed of its date."""

    cloud_depth: float           # how far a passing cloud dims the sun, 0-1
    cloud_threshold: float       # higher means fewer clouds
    phases: tuple[float, ...]
    events: tuple[tuple[int, int, float], ...]  # (start minute, minutes, watts)
    cooling: bool                # a hot, clear afternoon: the air conditioner runs

    @classmethod
    def for_date(cls, day: date) -> _Day:
        rng = random.Random(f"solar-monitor-demo-{day.isoformat()}")
        roll = rng.random()
        if roll < 0.6:      # clear
            depth, threshold = 0.05, 1.4
        elif roll < 0.9:    # broken cloud
            depth, threshold = rng.uniform(0.3, 0.5), rng.uniform(0.2, 0.7)
        else:               # overcast
            depth, threshold = rng.uniform(0.5, 0.65), -2.0
        cooling = roll < 0.6 and rng.random() < 0.8
        events = [
            (7 * 60 + rng.randint(0, 20), 4, 1500.0),                 # kettle
            (12 * 60 + rng.randint(0, 45), 3, 1100.0),                # microwave
            (18 * 60 + rng.randint(0, 60), 4, 1100.0),                # microwave
            (rng.randint(9 * 60, 11 * 60), 45, 450.0),                # washing machine
        ]
        for _ in range(3):                                            # well pump
            events.append((rng.randint(6 * 60, 21 * 60), 5, 700.0))
        if rng.random() < 0.5:
            events.pop(3)  # no laundry today
        phases = tuple(rng.uniform(0, 2 * math.pi) for _ in range(3))
        return cls(depth, threshold, phases, tuple(events), cooling)

    def sky(self, minute_of_day: float) -> float:
        """The share of clear-sky sunlight getting through, 0-1."""
        wave = (math.sin(minute_of_day / 7 + self.phases[0])
                + 0.6 * math.sin(minute_of_day / 19 + self.phases[1])
                + 0.4 * math.sin(minute_of_day / 47 + self.phases[2]))
        cover = max(0.0, min(1.0, (wave - self.cloud_threshold) / 0.6 + 0.5))
        return 1.0 - self.cloud_depth * cover


class SimulatedInverter:
    """The inverter, the array, the household and the bank, stepped through
    time in 30-second slots. Thread-safe: the collector and the admin probe
    may both ask it for frames."""

    def __init__(self, site: SiteConfig | None = None, *, capacity_ah: float = 180.0,
                 start: datetime | None = None, soc: float = 70.0):
        self.site = site or demo_site()
        self.protocol = Sumry8851Protocol()
        self.capacity_ah = capacity_ah  # what the bank really holds; the nameplate says 200
        self._lock = threading.Lock()
        self._days: dict[date, _Day] = {}
        self._config = self._as_reply(self.protocol.build_config_write(CONFIG_TEMPLATE, DEMO_SETTINGS))
        self._apply_setpoints()
        self.reset(start or datetime.now(UTC), soc)

    # --- State -------------------------------------------------------------

    def reset(self, start: datetime, soc: float = 70.0) -> None:
        with self._lock:
            self._time = start
            self._soc = soc
            self._stage = "bulk"
            self._absorbing_since: datetime | None = None
            self._reading = self._compute(start, advance=False)

    def _apply_setpoints(self) -> None:
        settings = self.protocol.decode(self._config).metrics
        self.bulk_volts = settings["bulk_charge_voltage_v"]
        self.float_volts = settings["float_charge_voltage_v"]
        self.max_charge_amps = settings["maximum_charge_current_a"]
        self.cutoff_amps = settings["charge_cutoff_current_a"]

    def settings(self) -> dict:
        """The configuration block as the protocol decodes it."""
        with self._lock:
            return self.protocol.decode(self._config).metrics

    def _day(self, day: date) -> _Day:
        if day not in self._days:
            self._days[day] = _Day.for_date(day)
        return self._days[day]

    # --- The model -----------------------------------------------------------

    def _site_clock(self, at: datetime) -> datetime:
        """Local solar time at the site. The household keeps it, so its day
        follows the site's sun whatever time zone the host runs in."""
        return (at + timedelta(hours=self.site.longitude / 15)).replace(tzinfo=None)

    def _pv_available(self, at: datetime) -> tuple[float, float]:
        """(watts the array could make, share of clear sky getting through)."""
        elevation, azimuth = solar_estimate.solar_position(self.site.latitude, self.site.longitude, at)
        if elevation <= 0:
            return 0.0, 1.0
        local = self._site_clock(at)
        sky = self._day(local.date()).sky(local.hour * 60 + local.minute + local.second / 60)
        plane = solar_estimate.plane_of_array_irradiance(
            elevation, azimuth, solar_estimate.clear_sky_ghi(elevation) * sky,
            self.site.tilt_degrees, self.site.azimuth_degrees,
        )
        # Heat and glass reflection cost more at low sun: 72% of nameplate
        # per kW/m^2 near the horizon, 84% with the sun high.
        efficiency = 0.72 + 0.12 * min(1.0, elevation / 45)
        return self.site.array_capacity_watts * plane / 1000 * efficiency, sky

    def _household(self, at: datetime) -> float:
        local = self._site_clock(at)
        minute = local.hour * 60 + local.minute
        seconds = at.timestamp()
        watts = 55.0                                   # router, the Pi, standby loads
        cycle = 47 if (minute < 6 * 60 or minute >= 23 * 60) else 38
        if (seconds / 60) % cycle < 13:                # fridge compressor
            watts += 115.0
        if 18 * 60 + 30 <= minute < 23 * 60:           # lights and television
            watts += 150.0
        elif 6 * 60 + 30 <= minute < 8 * 60:          # the morning
            watts += 90.0
        day = self._day(local.date())
        if day.cooling and 11 * 60 + 30 <= minute < 17 * 60 and (seconds / 60) % 35 < 25:
            watts += 650.0                             # air conditioner, cycling
        for start, length, extra in day.events:
            if start <= minute < start + length and (extra < 1000 or self._soc > 35):
                watts += extra
        return watts * (1 + 0.04 * _unit_noise(seconds, 1))

    def _step(self, at: datetime, pv_available: float, load: float, hours: float) -> dict:
        """Advance the bank by one slot and return the DC picture."""
        inverter_dc = load / INVERTER_EFFICIENCY + INVERTER_IDLE_WATTS
        surplus = pv_available - inverter_dc
        soc = self._soc
        ocv = _ocv(soc)
        capacity = self.capacity_ah
        if surplus > 0:
            wanted = min(surplus / (ocv + 1.0), self.max_charge_amps)
            # Lead-acid pushes back as it fills: the voltage at a given
            # current climbs steeply over the last fifth of the bank.
            bulk_volts = ocv + wanted * RESISTANCE_OHMS + wanted / capacity * (4.0 + 10.0 * (soc / 100) ** 6)
            if self._stage == "bulk" and bulk_volts >= self.bulk_volts:
                self._stage, self._absorbing_since = "absorption", at
            if self._stage == "bulk":
                amps, volts = wanted, bulk_volts
            else:
                target = self.bulk_volts if self._stage == "absorption" else self.float_volts
                share = 2.2 if self._stage == "absorption" else 0.6
                accepted = capacity * share * (1 - soc / 100) + (0.6 if self._stage == "absorption" else 0.3)
                amps = min(wanted, accepted)
                volts = target if accepted <= wanted else ocv + amps * RESISTANCE_OHMS + (target - ocv) * amps / accepted
                if (self._stage == "absorption" and accepted < self.cutoff_amps
                        and at - self._absorbing_since >= timedelta(minutes=10)):
                    self._stage = "float"
            pv_used = min(pv_available, inverter_dc + amps * volts)
        else:
            amps = surplus / max(ocv - 0.3, 20.0)
            volts = ocv + amps * RESISTANCE_OHMS - abs(amps) / capacity * 0.8
            pv_used = pv_available
            if soc < 96:
                self._stage = "bulk"
        stored = amps * hours * (CHARGE_EFFICIENCY if amps > 0 else 1.0)
        self._soc = max(0.0, min(100.0, soc + stored / capacity * 100))
        return {"amps": amps, "volts": volts, "pv_used": pv_used}

    def _compute(self, at: datetime, *, advance: bool = True) -> dict:
        pv_available, _sky = self._pv_available(at)
        load = self._household(at)
        hours = STEP.total_seconds() / 3600
        if advance:
            dc = self._step(at, pv_available, load, hours)
        else:
            dc = {"amps": 0.0, "volts": _ocv(self._soc), "pv_used": 0.0}
        seconds = at.timestamp()
        pv_watts = dc["pv_used"] if pv_available > 5 else 0.0
        curtailed = 1 - (pv_watts / pv_available) if pv_available > 5 else 0.0
        if pv_available > 5:
            vmp = self.site.vmp_volts * (0.9 + 0.1 * min(1.0, pv_available / self.site.array_capacity_watts))
            pv_volts = vmp + (self.site.voc_volts - vmp) * min(1.0, curtailed * 1.6)
        else:
            pv_volts = max(0.0, 2.5 + 2 * _unit_noise(seconds, 2))
        local = self._site_clock(at)
        hour = local.hour + local.minute / 60
        ambient = 21 + 7 * math.sin((hour - 9) / 24 * 2 * math.pi)
        stage_heat = (load + pv_watts) / 90
        ntc = [round(ambient + stage_heat + offset) for offset in (2.0, 6.0, 1.0)]
        fan = 0 if max(ntc) < 34 else min(100, 30 + (max(ntc) - 40) * 7 if max(ntc) > 40 else 30)
        volts_out = 120.0 + 0.4 * _unit_noise(seconds, 3)
        va = load / (0.9 + 0.06 * _unit_noise(seconds, 4) + 0.03)
        return {
            "output_voltage": round(volts_out, 1),
            "output_current": round(va / volts_out, 2),
            "output_frequency": round(60.0 + 0.04 * _unit_noise(seconds, 5), 2),
            "inverter_va": round(va + 12),
            "ac_load_va": round(va),
            "ac_load_watts": round(load),
            "ac_load_current": round(va / volts_out, 2),
            "grid_voltage": 0.0,
            "grid_frequency": 0.0,
            "battery_voltage": round(dc["volts"] + 0.01 * _unit_noise(seconds, 6), 2),
            "battery_current": round(dc["amps"], 1),
            "pv_voltage": round(pv_volts, 1),
            "pv_current": round(pv_watts / pv_volts, 2) if pv_volts > 50 else 0.0,
            "pv_watts": round(pv_watts),
            "bus_voltage": round(282.0 + load / 400 + 0.6 * _unit_noise(seconds, 7), 1),
            "fan1_percent": fan,
            "fan2_percent": fan,
            "ntc2_temperature": ntc[0],
            "ntc3_temperature": ntc[1],
            "ntc4_temperature": ntc[2],
            "bts_temperature": 25,
        }

    def reading(self, at: datetime) -> dict:
        """The decoded status reading at `at`, advancing the model to it in
        30-second slots. Asking for an earlier time returns the latest."""
        with self._lock:
            while self._time + STEP <= at:
                self._time += STEP
                self._reading = self._compute(self._time)
            return dict(self._reading)

    def cloud_cover(self, lat: float, lon: float) -> dict[str, float]:
        """Hourly cloud cover for the estimator, in the form Open-Meteo gives
        it, derived from the same sky the readings come from."""
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        hours = {}
        for offset in range(-24 * 8, 24 * 3):
            hour = now + timedelta(hours=offset)
            local = self._site_clock(hour)
            day = self._day(local.date())
            minute = local.hour * 60
            sky = sum(day.sky(minute + m) for m in range(0, 60, 5)) / 12
            loss = min(0.97, max(0.0, 1 - sky))
            # Invert the estimator's Kasten & Czeplak attenuation.
            hours[hour.strftime("%Y-%m-%dT%H:00")] = round(100 * (loss / 0.75) ** (1 / 3.4), 1)
        return hours

    # --- Frames ----------------------------------------------------------------

    def respond(self, request: bytes, at: datetime | None = None) -> bytes:
        """What the inverter sends back to one outbound frame."""
        at = at or datetime.now(UTC)
        if request == self.protocol.query("status_candidate_0003").frame:
            return encode_status(self.reading(at))
        if request == self.protocol.query("current_settings_0200").frame:
            with self._lock:
                return self._config
        if request[:4] == bytes.fromhex("88510010"):
            with self._lock:
                try:
                    self.protocol.assert_write_frame(request, self._config)
                except PermissionError:
                    return b""  # the real inverter ignores a frame it will not take
                self._config = self._as_reply(request)
                self._apply_setpoints()
            acknowledgement = request[:8]
            return acknowledgement + struct.pack("<H", _crc16_modbus(acknowledgement))
        return b""

    @staticmethod
    def _as_reply(write: bytes) -> bytes:
        frame = bytearray(write)
        frame[2:4] = bytes.fromhex("0003")
        struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
        return bytes(frame)


def encode_status(metrics: dict) -> bytes:
    """A status frame carrying `metrics`, laid out by the decoder's own table."""
    frame = bytearray(STATUS_TEMPLATE)
    for name, (offset, layout, divisor) in STATUS_FIELDS.items():
        struct.pack_into(layout, frame, offset, round(metrics[name] * divisor))
    struct.pack_into("<H", frame, len(frame) - 2, _crc16_modbus(bytes(frame[:-2])))
    return bytes(frame)


# --- The shared instance ---------------------------------------------------------
# A `sim://` port reaches whichever inverter is installed here. Opening one
# before anything was installed gets a fresh default, so `diagnostic.py
# --port sim://` works on its own.

_installed: SimulatedInverter | None = None
_install_lock = threading.Lock()


def install(inverter: SimulatedInverter | None) -> None:
    global _installed
    with _install_lock:
        _installed = inverter


def installed() -> SimulatedInverter:
    global _installed
    with _install_lock:
        if _installed is None:
            _installed = SimulatedInverter()
        return _installed


def backfill(database, inverter: SimulatedInverter, *, days: int = 7, until: datetime | None = None) -> int:
    """Write `days` of readings ending at `until` straight into the database,
    as if the collector had been running all along. Returns the count."""
    until = until or datetime.now(UTC)
    start = until - timedelta(days=days)
    # A day of warm-up that is not stored, so the stored week opens with the
    # bank wherever this household's routine leaves it rather than wherever
    # the model happened to start.
    inverter.reset(start - timedelta(days=1))
    inverter.reading(start)
    database.save_inverter_config(start.isoformat(), inverter.settings())
    rows = []
    at = start + STEP
    snapshot_every = timedelta(hours=6)
    next_snapshot = start + snapshot_every
    while at <= until:
        rows.append((at.isoformat(), inverter.reading(at)))
        if at >= next_snapshot:
            database.save_inverter_config(at.isoformat(), inverter.settings())
            next_snapshot += snapshot_every
        at += STEP
    database.save_samples(rows)
    return len(rows)


# --- Demo mode -------------------------------------------------------------------

DEMO_DIRECTORY = Path("instance/demo")
DEMO_ADMIN = ("demo", "demo-password")


def prepare_demo(directory: Path = DEMO_DIRECTORY, *, days: int = 7) -> tuple[AppConfig, Database]:
    """A fresh demo database holding `days` of history and an admin account,
    and the configuration that serves it. The database is rebuilt on every
    start so each run begins from the same story; it lives apart from any
    real installation's, and the web server binds to localhost only."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "demo.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    site = demo_site()
    inverter = SimulatedInverter(site)
    install(inverter)
    solar_estimate.use_weather("simulated", inverter.cloud_cover)
    database = Database(str(path))
    backfill(database, inverter, days=days)
    username, password = DEMO_ADMIN
    database.create_admin(username, generate_password_hash(password), datetime.now(UTC).isoformat())
    config = AppConfig(
        serial=SerialConfig(port="sim://"),
        web=WebConfig(host="127.0.0.1"),
        storage=StorageConfig(str(path)),
        site=site,
    )
    return config, database

