from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template

from .db import Database


def create_app(database: Database) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database

    @app.get("/")
    def dashboard():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify({
            "sample": database.latest_sample(),
            "diagnostic": database.latest_diagnostic(),
            "read_only": True,
        })

    @app.get("/api/history")
    def history():
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        return jsonify(database.history(since.isoformat()))

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "read_only": True})

    return app

