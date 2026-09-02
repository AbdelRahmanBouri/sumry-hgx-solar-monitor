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


class ReadOnlyProtocol:
    """Protocol interface whose outbound surface is a fixed safe-query registry."""

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

