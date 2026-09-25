from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Query:
    name: str
    frame: bytes
    description: str


@dataclass(frozen=True)
class DecodeResult:
    metrics: dict[str, Any]
    valid: bool
    note: str


class Protocol:
    """Protocol interface. Outbound reads are an allow-list: `queries` is
    every frame `exchange` may send, and `assert_read_only_frame` refuses any
    other. A protocol that can change settings adds `build_config_write` and
    `assert_write_frame`, which gate the one write path separately."""

    name = "base"
    queries: tuple[Query, ...] = ()

    def query(self, name: str) -> Query:
        for item in self.queries:
            if item.name == name:
                return item
        raise ValueError(f"Query {name!r} is not in the read-only allow-list")

    def assert_read_only_frame(self, frame: bytes) -> None:
        if frame not in {query.frame for query in self.queries}:
            raise PermissionError("Outbound frame rejected: not an allow-listed read query")

    def decode(self, frame: bytes) -> DecodeResult:
        return DecodeResult({}, False, "No decoder has been confirmed for this firmware")
