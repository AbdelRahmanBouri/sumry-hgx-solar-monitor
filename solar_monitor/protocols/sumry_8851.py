from __future__ import annotations

from .base import DecodeResult, Query, ReadOnlyProtocol


class Sumry8851ProbeProtocol(ReadOnlyProtocol):
    """Conservative HGX probe profile; decoding awaits captured hardware frames."""

    name = "sumry_8851_probe"
    queries = (
        Query(
            name="status_candidate_0003",
            frame=bytes.fromhex("88 51 00 03 00 00 00 00 4D 08"),
            description="Candidate binary 0x8851 status query supplied for HGX firmware",
        ),
    )

    def decode(self, frame: bytes) -> DecodeResult:
        # Preserve all replies, but intentionally avoid guessing field offsets.
        return DecodeResult(
            metrics={},
            valid=bool(frame),
            note="Raw reply captured; HGX field mapping is not yet confirmed",
        )

