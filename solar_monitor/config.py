from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    import tomli as tomllib


@dataclass(frozen=True)
class SerialConfig:
    port: str = "auto"
    baudrate: int = 9600
    timeout_seconds: float = 1.0
    poll_seconds: int = 30
    protocol: str = "sumry_8851_probe"


@dataclass(frozen=True)
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 5000


@dataclass(frozen=True)
class StorageConfig:
    database: str = "instance/solar-monitor.sqlite3"


@dataclass(frozen=True)
class AppConfig:
    serial: SerialConfig = SerialConfig()
    web: WebConfig = WebConfig()
    storage: StorageConfig = StorageConfig()


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None or not Path(path).exists():
        return AppConfig()
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return AppConfig(
        serial=SerialConfig(**raw.get("serial", {})),
        web=WebConfig(**raw.get("web", {})),
        storage=StorageConfig(**raw.get("storage", {})),
    )
