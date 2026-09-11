"""Builds the dashboard payload from the bot's SQLite database (shared by the
local dashboard and the cloud push)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from data_engine import DataEngine


def _rows(db: DataEngine, sql: str, cols: list[str]) -> list[dict]:
    return [dict(zip(cols, r)) for r in db.query(sql)]


def build_payload(db: DataEngine) -> dict:
    signals = _rows(db, "SELECT ts, direction, outcome, checks FROM signals ORDER BY id DESC LIMIT 12",
                    ["ts", "direction", "outcome", "checks"])
    for s in signals:
        try:
            s["checks"] = json.loads(s["checks"] or "{}")
        except ValueError:
            s["checks"] = {}
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "live": db.get_state("live_status"),
        "signals": signals,
        "trades": _rows(db, "SELECT ts, ticket, direction, volume, entry, sl, tp, risk_amount, status, detail "
                            "FROM trades ORDER BY id DESC LIMIT 12",
                        ["ts", "ticket", "direction", "volume", "entry", "sl", "tp", "risk", "status", "detail"]),
        "audit": _rows(db, "SELECT ts, level, message FROM audit ORDER BY id DESC LIMIT 8",
                       ["ts", "level", "message"]),
    }
