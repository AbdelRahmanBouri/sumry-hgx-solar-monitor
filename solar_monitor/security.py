from __future__ import annotations

import os
from pathlib import Path
import secrets


def load_or_create_secret(directory: Path, filename: str, length: int = 48) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(length)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value

