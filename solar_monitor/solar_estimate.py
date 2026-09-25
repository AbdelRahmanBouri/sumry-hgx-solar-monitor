"""GPS + weather based clear-sky/estimated PV output modelling.

Combines a standalone sun-position calculation (NOAA's simplified solar
position algorithm) with live cloud-cover data from Open-Meteo's free,
key-less forecast API to estimate how many watts a fixed array at a given
latitude/longitude should be producing right now, in the recent past, and
over the next few hours.

No third-party HTTP library is used; requests go through urllib so this
stays a stdlib-only module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_SECONDS = 8
WEATHER_CACHE_SECONDS = 1800  # Cloud-cover forecasts only update hourly upstream.
# After a failed fetch, wait this long before trying again. An off-grid Pi
# loses its internet more often than its power, and every attempt against a
# dead link can hold a request for the whole timeout.
WEATHER_RETRY_SECONDS = 300
WEATHER_PAST_DAYS = 7  # Long enough to cover the efficiency-learning window.
SOLAR_CONSTANT = 1361.0  # W/m^2, extraterrestrial irradiance.
GROUND_ALBEDO = 0.25  # Dry desert dirt and gravel around this installation.

# Learned efficiency. The clear-sky model gives the *shape* of a day; how much
# of that shape a real array delivers depends on heat, glass reflection at low
# sun, haze, neighbouring roofs on the horizon and the wiring -- none of which
# a single hand-set "performance ratio" can capture, because they vary with
# the height of the sun. So the level is learned instead: for each band of sun
# elevation, the share of the clear-sky model the array actually produced
# while nothing was holding it back.
EFFICIENCY_BAND_DEGREES = 10
LEARNING_DAYS = 7
MIN_BAND_SAMPLES = 20
MIN_TOTAL_SAMPLES = 60
ENVELOPE_PERCENTILE = 0.90
MIN_MODEL_FRACTION = 0.08  # Ignore readings where the model itself is near zero.
# The bank holds at 27.0 V float / 28.2 V absorption. Below the lower plateau
# the charger is in bulk, passing on everything the panels make, so those
# readings show the array's true capability.
HOLDING_VOLTS = 26.9


@dataclass
class _Forecast:
    cover: dict[str, float] | None = None  # the last forecast that arrived
    fetched_at: float | None = None        # monotonic time it arrived
    attempted_at: float | None = None      # monotonic time of the last try, good or bad


_weather_cache: dict[str, _Forecast] = {}


def solar_position(lat_deg: float, lon_deg: float, when: datetime) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg) for the given place and instant.

    Implements NOAA's simplified solar position formulas (as used by the
    NOAA Solar Calculator), accurate to well within a degree -- ample for
    estimating panel output from clear-sky irradiance.
    """
    when = when.astimezone(UTC)
    day_of_year = when.timetuple().tm_yday
    hour_utc = when.hour + when.minute / 60 + when.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour_utc - 12) / 24)

    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )

    time_offset = eqtime + 4 * lon_deg
    # Wrap at UTC midnight. Without this modulo, western longitudes produce
    # a valid elevation (cosine is periodic) but the wrong afternoon azimuth
    # after the UTC date rolls over.
    true_solar_time = (hour_utc * 60 + time_offset) % 1440
    hour_angle_deg = (true_solar_time / 4) - 180
    hour_angle = math.radians(hour_angle_deg)

    lat = math.radians(lat_deg)
    cos_zenith = (
        math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90 - math.degrees(zenith)

    sin_zenith = math.sin(zenith)
    if sin_zenith == 0:
        azimuth = 180.0
    else:
        cos_az = (math.sin(decl) - math.sin(lat) * cos_zenith) / (math.cos(lat) * sin_zenith)
        cos_az = max(-1.0, min(1.0, cos_az))
        azimuth = math.degrees(math.acos(cos_az))
        if hour_angle > 0:
            azimuth = 360 - azimuth

    return elevation, azimuth


def clear_sky_ghi(elevation_deg: float) -> float:
    """Clear-sky global horizontal irradiance in W/m^2 (Haurwitz 1945 model)."""
    if elevation_deg <= 0:
        return 0.0
    cos_z = math.cos(math.radians(90 - elevation_deg))
    return max(0.0, 1098.0 * cos_z * math.exp(-0.057 / max(cos_z, 0.001)))


def erbs_diffuse_fraction(clearness_index: float) -> float:
    """Share of horizontal irradiance arriving as diffuse sky light.

    Erbs et al. (1982), the standard empirical decomposition: a clear sky
    (high clearness index) is mostly direct beam, an overcast one is nearly
    all diffuse. This is what lets a tilted panel be modelled at all -- beam
    light cares about the angle it strikes the glass, diffuse light barely does.
    """
    kt = max(0.0, min(1.0, clearness_index))
    if kt <= 0.22:
        return 1 - 0.09 * kt
    if kt <= 0.80:
        return (0.9511 - 0.1604 * kt + 4.388 * kt ** 2
                - 16.638 * kt ** 3 + 12.336 * kt ** 4)
    return 0.165


def plane_of_array_irradiance(
    elevation_deg: float,
    azimuth_deg: float,
    ghi: float,
    tilt_deg: float,
    panel_azimuth_deg: float,
    albedo: float = GROUND_ALBEDO,
) -> float:
    """Irradiance in W/m^2 on a tilted panel, from horizontal irradiance.

    Isotropic sky (Liu & Jordan) plus ground reflection: beam light is
    projected onto the panel by its angle of incidence, diffuse light is
    scaled by how much sky the panel still sees, and a tilted panel also
    picks up light bouncing off the ground.
    """
    if elevation_deg <= 0 or ghi <= 0:
        return 0.0
    zenith = math.radians(90 - elevation_deg)
    cos_zenith = math.cos(zenith)
    extraterrestrial = SOLAR_CONSTANT * cos_zenith
    clearness = min(1.0, ghi / extraterrestrial) if extraterrestrial > 0 else 0.0
    diffuse = ghi * erbs_diffuse_fraction(clearness)
    # Guard the low-sun divide: near sunrise a tiny cos(zenith) would blow the
    # beam component up far beyond anything physical.
    direct_normal = (ghi - diffuse) / max(cos_zenith, 0.05)

    tilt = math.radians(tilt_deg)
    cos_incidence = (
        cos_zenith * math.cos(tilt)
        + math.sin(zenith) * math.sin(tilt)
        * math.cos(math.radians(azimuth_deg - panel_azimuth_deg))
    )
    beam = direct_normal * max(0.0, cos_incidence)
    sky = diffuse * (1 + math.cos(tilt)) / 2
    ground = ghi * albedo * (1 - math.cos(tilt)) / 2
    return beam + sky + ground


# Fraction of a split array's independent output that survives when every
# series string straddles both roof faces, keyed on the weaker face's share of
# the stronger one's irradiance. From a single-diode model of this array's
# 96-cell modules wired 4S2P with two modules per face: the shared string
# current is pinned by the shaded pair, so the sunlit pair cannot deliver what
# it is capable of. The curve is worst near a 0.45 ratio and recovers below it,
# where the bypass diodes give up on the weak pair entirely. It depends only on
# the ratio, not the absolute irradiance (verified to within 0.1%).
SERIES_MISMATCH_RETAINED = (
    (0.00, 0.963), (0.05, 0.923), (0.10, 0.884), (0.15, 0.847), (0.20, 0.812),
    (0.25, 0.780), (0.30, 0.750), (0.35, 0.722), (0.40, 0.696), (0.45, 0.672),
    (0.50, 0.706), (0.55, 0.751), (0.60, 0.793), (0.65, 0.831), (0.70, 0.866),
    (0.75, 0.899), (0.80, 0.928), (0.85, 0.954), (0.90, 0.976), (0.95, 0.993),
    (1.00, 1.000),
)


def series_mismatch_factor(weak: float, strong: float) -> float:
    """Output retained when a series string spans both faces of a split array.

    Wiring each string to a single face avoids this entirely; it is only a
    loss when the strings straddle, because series modules must share one
    current and the shaded ones set it.
    """
    if strong <= 0:
        return 1.0
    ratio = max(0.0, min(1.0, weak / strong))
    for index in range(1, len(SERIES_MISMATCH_RETAINED)):
        low_ratio, low_value = SERIES_MISMATCH_RETAINED[index - 1]
        high_ratio, high_value = SERIES_MISMATCH_RETAINED[index]
        if ratio <= high_ratio:
            span = high_ratio - low_ratio
            weight = (ratio - low_ratio) / span if span else 0.0
            return low_value + (high_value - low_value) * weight
    return 1.0


def cloud_attenuation(cloud_cover_percent: float) -> float:
    """Fraction of clear-sky GHI that still reaches the ground under cloud.

    Kasten & Czeplak (1980): a widely used empirical fit relating fractional
    cloud cover to the reduction in surface solar radiation.
    """
    fraction = max(0.0, min(100.0, cloud_cover_percent)) / 100
    return max(0.03, 1 - 0.75 * (fraction ** 3.4))


def _cache_key(lat: float, lon: float) -> str:
    # Round to ~1km so nearby requests (and repeated saves of the same
    # coordinates) reuse one cached forecast instead of refetching.
    return f"{round(lat, 2)},{round(lon, 2)}"


def fetch_cloud_cover(lat: float, lon: float, *, force: bool = False) -> dict[str, float] | None:
    """Hourly cloud-cover percent keyed by UTC hour ("YYYY-MM-DDTHH:00"), or
    None if no forecast has ever been retrieved (e.g. no internet).

    A fresh forecast is reused for `WEATHER_CACHE_SECONDS`. When a fetch
    fails, the last good forecast is served -- its hours are still the hours
    it describes -- and no new attempt is made for `WEATHER_RETRY_SECONDS`.
    Before that back-off, a dead link was retried by every caller, and one
    /api/estimate request asks four times.
    """
    entry = _weather_cache.setdefault(_cache_key(lat, lon), _Forecast())
    now = time.monotonic()
    if not force:
        if entry.fetched_at is not None and now - entry.fetched_at < WEATHER_CACHE_SECONDS:
            return entry.cover
        if entry.attempted_at is not None and now - entry.attempted_at < WEATHER_RETRY_SECONDS:
            return entry.cover

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "cloud_cover",
        "past_days": str(WEATHER_PAST_DAYS),
        "forecast_days": "3",
        "timezone": "UTC",
    }
    url = f"{WEATHER_URL}?{urllib.parse.urlencode(params)}"
    entry.attempted_at = now
    try:
        with urllib.request.urlopen(url, timeout=WEATHER_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
        hourly = payload["hourly"]
        cover = {t: c for t, c in zip(hourly["time"], hourly["cloud_cover"]) if c is not None}
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, OSError):
        return entry.cover
    entry.cover, entry.fetched_at = cover, now
    return cover


def _hour_bucket(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:00")


def array_irradiance(
    elevation_deg: float,
    azimuth_deg: float,
    ghi: float,
    tilt_degrees: float | None,
    azimuth_degrees: float | None,
    dual_slope: bool = False,
    split_strings: bool = False,
) -> float:
    """Irradiance the array collects per m^2, W/m^2, from horizontal GHI.

    With no tilt or azimuth the array is treated as flat. `dual_slope` splits
    it evenly over both faces of a pitched roof -- `azimuth_degrees` and its
    opposite -- which is how a flush-mounted gable installation sits. The
    two faces largely cancel, so a symmetric gable collects within a couple
    of percent of a flat array whatever the pitch.

    `split_strings` says each series string straddles both faces rather than
    living on one, and applies the resulting mismatch loss.
    """
    if tilt_degrees is None or azimuth_degrees is None:
        return ghi
    front = plane_of_array_irradiance(elevation_deg, azimuth_deg, ghi, tilt_degrees, azimuth_degrees)
    if not dual_slope:
        return front
    back = plane_of_array_irradiance(
        elevation_deg, azimuth_deg, ghi, tilt_degrees, (azimuth_degrees + 180) % 360
    )
    combined = (front + back) / 2
    if split_strings:
        combined *= series_mismatch_factor(min(front, back), max(front, back))
    return combined


def _band_of(elevation_deg: float) -> int:
    return int(max(0.0, elevation_deg) // EFFICIENCY_BAND_DEGREES) * EFFICIENCY_BAND_DEGREES


def efficiency_at(bands: dict[str, float], elevation_deg: float) -> float:
    """The learned factor for a sun height, interpolated between band centres
    and held flat beyond the outermost bands."""
    if not bands:
        return 1.0
    centres = sorted((int(key) + EFFICIENCY_BAND_DEGREES / 2, value) for key, value in bands.items())
    if elevation_deg <= centres[0][0]:
        return centres[0][1]
    if elevation_deg >= centres[-1][0]:
        return centres[-1][1]
    for (low_deg, low_value), (high_deg, high_value) in zip(centres, centres[1:]):
        if low_deg <= elevation_deg <= high_deg:
            weight = (elevation_deg - low_deg) / (high_deg - low_deg)
            return low_value + (high_value - low_value) * weight
    return centres[-1][1]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def learn_efficiency(
    samples: list[dict],
    lat: float,
    lon: float,
    capacity_watts: float,
    tilt_degrees: float | None = None,
    azimuth_degrees: float | None = None,
    dual_slope: bool = False,
    cloud_by_hour: dict[str, float] | None = None,
) -> dict | None:
    """Learn, per band of sun elevation, what share of the clear-sky model
    this array delivers when nothing is throttling it.

    Only readings taken while the battery was below its holding voltage
    count: once the bank holds a plateau the charger throttles, and those
    readings say nothing about the panels. Within a band the
    factor is a high percentile of the observed ratios rather than the mean,
    so the line is an envelope of demonstrated output -- passing clouds and
    the odd jittery reading fall below it instead of dragging it down.

    Returns None when there is not yet enough unconstrained data; the caller
    then falls back to its configured performance ratio.
    """
    if capacity_watts <= 0:
        return None
    ratios_by_band: dict[int, list[float]] = {}
    days: set[str] = set()
    for sample in samples:
        try:
            when = datetime.fromisoformat(sample["captured_at"])
            pv = float(sample["pv_watts"])
            volts = float(sample["battery_voltage"])
        except (KeyError, TypeError, ValueError):
            continue
        # Below the plateau the charger is in bulk and tracking the panels'
        # maximum power, whatever the current: little of it reaching the
        # battery just means the home is using the solar. At or above it the
        # charger regulates voltage instead, and even a bank still taking
        # 15 A in absorption is being throttled -- those readings sat 12%
        # under the free ones when they were let in.
        if volts >= HOLDING_VOLTS:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        elevation, azimuth = solar_position(lat, lon, when)
        if elevation < 3:
            continue
        cloud_percent = (cloud_by_hour or {}).get(_hour_bucket(when))
        attenuation = cloud_attenuation(cloud_percent) if cloud_percent is not None else 1.0
        plane = array_irradiance(
            elevation, azimuth, clear_sky_ghi(elevation) * attenuation,
            tilt_degrees, azimuth_degrees, dual_slope,
        )
        modelled = capacity_watts * plane / 1000
        if modelled < capacity_watts * MIN_MODEL_FRACTION:
            continue
        ratio = pv / modelled
        if not 0 < ratio < 1.3:
            continue
        ratios_by_band.setdefault(_band_of(elevation), []).append(ratio)
        days.add(when.astimezone(UTC).strftime("%Y-%m-%d"))

    observed = {
        band: round(min(1.1, max(0.3, _percentile(ratios, ENVELOPE_PERCENTILE))), 3)
        for band, ratios in ratios_by_band.items()
        if len(ratios) >= MIN_BAND_SAMPLES
    }
    total = sum(len(ratios) for band, ratios in ratios_by_band.items() if band in observed)
    if not observed or total < MIN_TOTAL_SAMPLES:
        return None
    return {
        "bands": {str(band): value for band, value in sorted(observed.items())},
        "samples": total,
        "days": len(days),
        "peak_factor": max(observed.values()),
    }


def estimate_series(
    lat: float,
    lon: float,
    capacity_watts: float,
    performance_ratio: float,
    start: datetime,
    end: datetime,
    step_minutes: int = 10,
    tilt_degrees: float | None = None,
    azimuth_degrees: float | None = None,
    dual_slope: bool = False,
    split_strings: bool = False,
    efficiency: dict[str, float] | None = None,
) -> tuple[list[dict], str]:
    """Modelled output in watts from `start` to `end` (inclusive-ish).

    Geometry (`tilt_degrees`, `azimuth_degrees`, `dual_slope`,
    `split_strings`) is described under `array_irradiance`.

    The level comes from `efficiency` -- learned per sun-elevation band by
    `learn_efficiency` -- when given, and otherwise from the flat
    `performance_ratio`.

    Returns (points, weather_source) where weather_source is "open-meteo"
    when live cloud data was used, otherwise "clear-sky-only".
    """
    cloud_by_hour = fetch_cloud_cover(lat, lon)
    weather_source = "open-meteo" if cloud_by_hour else "clear-sky-only"
    now = datetime.now(UTC)
    geometry = {
        "tilt_degrees": tilt_degrees, "azimuth_degrees": azimuth_degrees,
        "dual_slope": dual_slope, "split_strings": split_strings,
    }

    points: list[dict] = []
    step = timedelta(minutes=max(1, step_minutes))
    cursor = start
    while cursor <= end:
        elevation, azimuth = solar_position(lat, lon, cursor)
        clear_sky = clear_sky_ghi(elevation)
        cloud_percent = (cloud_by_hour or {}).get(_hour_bucket(cursor))
        attenuation = cloud_attenuation(cloud_percent) if cloud_percent is not None else 1.0
        factor = efficiency_at(efficiency, elevation) if efficiency else performance_ratio

        # Attenuate first, then transpose: thicker cloud lowers the clearness
        # index, which Erbs turns into a larger diffuse share, so an overcast
        # sky correctly stops caring about panel angle.
        clear_watts, estimated_watts = (
            _array_watts(horizontal, elevation, azimuth, geometry, capacity_watts, factor)
            for horizontal in (clear_sky, clear_sky * attenuation)
        )
        points.append({
            "timestamp": cursor.isoformat(),
            "elevation_deg": round(elevation, 2),
            "azimuth_deg": round(azimuth, 2),
            "cloud_cover_percent": cloud_percent,
            "clear_sky_watts": clear_watts,
            "estimated_watts": estimated_watts,
            "forecast": cursor > now,
        })
        cursor += step
    return points, weather_source


def _array_watts(
    horizontal: float, elevation: float, azimuth: float, geometry: dict, capacity_watts: float, factor: float,
) -> float:
    """DC watts from horizontal irradiance, capped a little above nameplate."""
    plane = array_irradiance(elevation, azimuth, horizontal, **geometry)
    return round(max(0.0, min(capacity_watts * 1.05, capacity_watts * (plane / 1000) * factor)), 1)
