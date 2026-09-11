"""
Dashboard for xau_rulebot.

Local (on the PC running the bot):
    python dashboard.py                 -> http://127.0.0.1:5000

Cloud (Render): set DASHBOARD_MODE=cloud. The bot pushes its status to
/api/push every few seconds; the page shows the latest push.
    gunicorn dashboard:app --workers 1 --threads 4

Env vars (cloud):
    DASHBOARD_PUSH_KEY   secret the bot must send (same value in the bot's .env)
    DASHBOARD_PASSWORD   password for viewing the page (username: anything)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

WEB_DIR = Path(__file__).resolve().parent / "web"
CLOUD = os.getenv("DASHBOARD_MODE", "local").strip().lower() == "cloud"
PUSH_KEY = os.getenv("DASHBOARD_PUSH_KEY", "").strip()
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
CACHE_FILE = Path(os.getenv("DASHBOARD_CACHE", "/tmp/goldbot_latest.json"))


def create_app(db=None, cloud: bool = CLOUD) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    latest = {"payload": None}
    lock = threading.Lock()

    if cloud:
        try:
            latest["payload"] = json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    else:
        from data_engine import DataEngine
        db = db or DataEngine()

    def authorized() -> bool:
        if not PASSWORD:
            return True
        auth = request.authorization
        return bool(auth and auth.password and hmac.compare_digest(auth.password, PASSWORD))

    def need_login():
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Gold bot"'})

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        if not authorized():
            return need_login()
        return send_file(WEB_DIR / "dashboard.html")

    @app.get("/api/status")
    def status():
        if not authorized():
            return need_login()
        if not cloud:
            from status_api import build_payload
            return jsonify(build_payload(db))
        with lock:
            payload = dict(latest["payload"] or {"live": None, "signals": [], "trades": [], "audit": []})
        payload["now"] = datetime.now(timezone.utc).isoformat()
        return jsonify(payload)

    @app.post("/api/push")
    def push():
        if not cloud:
            return {"error": "push is only used in cloud mode"}, 404
        key = request.headers.get("X-Push-Key", "")
        if not PUSH_KEY or not hmac.compare_digest(key, PUSH_KEY):
            return {"error": "bad push key"}, 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "live" not in payload:
            return {"error": "bad payload"}, 400
        with lock:
            latest["payload"] = payload
        try:
            CACHE_FILE.write_text(json.dumps(payload))
        except OSError:
            pass
        return {"ok": True}

    return app


app = create_app() if CLOUD else None   # gunicorn entry point on Render


def main() -> None:
    parser = argparse.ArgumentParser(description="xau_rulebot dashboard")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    print(f"Dashboard: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    create_app(cloud=CLOUD).run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
