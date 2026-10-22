from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import secrets
from time import time as wall_time

from flask import Flask, request
from flask.sessions import SecureCookieSessionInterface

from .db import Database


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



class LoginLimiter:
    """Atomic fixed-window budgets shared by threads and processes via SQLite.

    Call before password hashing. Expired buckets are removed on each attempt;
    the global budget bounds source/account bucket growth during a window.
    Keys are hashed so this table does not retain addresses or usernames.
    """

    def __init__(self, database: Database, window_seconds: int = 900) -> None:
        self.database = database
        self.window_seconds = window_seconds
        with database.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS login_limits "
                               "(key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires REAL NOT NULL)")

    @staticmethod
    def key(scope: str, identity: str) -> str:
        return hashlib.sha256(f"{scope}:{identity}".encode()).hexdigest()

    def take(self, scope: str, identity: str, limit: int) -> int:
        """Reserve an attempt, returning Retry-After seconds if exhausted."""
        at = wall_time()
        key = self.key(scope, identity)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM login_limits WHERE expires <= ?", (at,))
            row = connection.execute("SELECT attempts, expires FROM login_limits WHERE key = ?", (key,)).fetchone()
            if row and row[0] >= limit:
                return max(1, math.ceil(float(row[1]) - at))
            connection.execute(
                "INSERT INTO login_limits (key, attempts, expires) VALUES (?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET attempts = attempts + 1", (key, at + self.window_seconds),
            )
        return 0

    def clear(self, scope: str, identity: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM login_limits WHERE key = ?", (self.key(scope, identity),))


class TransportSessionInterface(SecureCookieSessionInterface):
    """Secure on direct HTTPS, or explicitly when TLS terminates at a proxy.

    Forwarded headers are deliberately not trusted by the application.
    """

    def get_cookie_secure(self, app: Flask) -> bool:
        return bool(app.config["SESSION_COOKIE_SECURE"] or request.is_secure)
