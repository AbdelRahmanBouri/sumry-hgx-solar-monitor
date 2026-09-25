from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
import tomllib
from typing import Any, TypeVar

DEFAULT_PATH = Path("config.toml")


@dataclass(frozen=True)
class SerialConfig:
    port: str = "auto"
    baudrate: int = 9600
    timeout_seconds: float = 1.0
    poll_seconds: int = 30
    protocol: str = "sumry_8851"


@dataclass(frozen=True)
class WebConfig:
    """How the dashboard is served.

    `expose_network_info` defaults to off because the dashboard needs no
    login: with it on, anyone who can reach the page learns the hostname,
    the LAN address and the Wi-Fi SSID. An operator who trusts everyone on
    their network can turn it back on; the admin portal shows those fields
    either way, behind a login.
    """

    host: str = "0.0.0.0"
    port: int = 5000
    expose_network_info: bool = False


@dataclass(frozen=True)
class StorageConfig:
    database: str = "instance/solar-monitor.sqlite3"


@dataclass(frozen=True)
class SiteConfig:
    """The installation's own profile: where it is and what hardware it runs.

    Everything here belongs to one house rather than to the software, so it
    lives in the config file and never in source. Every field defaults to
    "unknown": an absent `[site]` table means there is no profile, and the
    estimator stays off until an admin supplies a location and capacity.
    """

    latitude: float | None = None
    longitude: float | None = None
    array_capacity_watts: float | None = None
    performance_ratio: float | None = None
    tilt_degrees: float | None = None
    azimuth_degrees: float | None = None
    dual_slope: bool = False
    split_strings: bool = False
    array_description: str | None = None
    module_count: int | None = None
    module_watts: float | None = None
    wiring: str | None = None
    vmp_volts: float | None = None
    voc_volts: float | None = None
    imp_amps: float | None = None
    isc_amps: float | None = None
    inverter_model: str | None = None
    charge_limit_amps: float | None = None
    battery_description: str | None = None
    battery_nominal_volts: float | None = None
    battery_capacity_ah: float | None = None


@dataclass(frozen=True)
class AppConfig:
    serial: SerialConfig = SerialConfig()
    web: WebConfig = WebConfig()
    storage: StorageConfig = StorageConfig()
    site: SiteConfig = SiteConfig()


SectionT = TypeVar("SectionT")


def _section(section_type: type[SectionT], name: str, raw: dict[str, Any]) -> SectionT:
    """Build one table's dataclass, naming any key it does not know.

    Handing an unknown key straight to the dataclass raises a TypeError deep
    in generated code; a config typo deserves a message that says which
    table and which key.
    """
    values = raw.get(name, {})
    known = {field.name for field in fields(section_type)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(
            f"[{name}] has no setting named {unknown[0]!r}; "
            f"known settings are {', '.join(sorted(known))}"
        )
    return section_type(**values)


def load_config(path: str | Path | None = None, *, required: bool = False) -> AppConfig:
    """The configuration in `path`, or the defaults when there is none.

    With `required`, a missing file is an error instead. Callers set it when
    the path was named explicitly: quietly running on defaults looks like a
    working installation whose estimator has switched itself off, and says
    nothing about the typo that caused it.
    """
    if path is None or not Path(path).exists():
        if required:
            raise FileNotFoundError(f"Config file not found: {path}")
        return AppConfig()
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return AppConfig(
        serial=_section(SerialConfig, "serial", raw),
        web=_section(WebConfig, "web", raw),
        storage=_section(StorageConfig, "storage", raw),
        site=_section(SiteConfig, "site", raw),
    )
