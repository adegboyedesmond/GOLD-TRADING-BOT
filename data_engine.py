"""
SQLite data engine: trade log, news blocks, signal audit trail and persistent
system state (e.g. the daily drawdown breaker survives restarts).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, ticket INTEGER, direction TEXT, volume REAL,
    entry REAL, sl REAL, tp REAL, atr REAL, risk_amount REAL, equity REAL,
    status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS news_blocks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT, event_time TEXT, bar_time TEXT);
CREATE TABLE IF NOT EXISTS signals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, bar_time TEXT, direction TEXT, outcome TEXT, checks TEXT);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS system_state(
    key TEXT PRIMARY KEY, value TEXT, updated TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DataEngine:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
                conn.commit()
                return rows
            finally:
                conn.close()

    # ------------------------------------------------------------ logs
    def audit(self, level: str, message: str) -> None:
        self._execute("INSERT INTO audit(ts, level, message) VALUES (?,?,?)",
                      (_now(), level, message))

    def log_signal(self, bar_time: str, direction: str | None, outcome: str, checks: dict) -> None:
        self._execute("INSERT INTO signals(ts, bar_time, direction, outcome, checks) VALUES (?,?,?,?,?)",
                      (_now(), bar_time, direction, outcome, json.dumps(checks, default=str)))

    def log_news_block(self, event_id: str, title: str, event_time: str, bar_time: str) -> None:
        self._execute("INSERT INTO news_blocks(ts, event_id, title, event_time, bar_time) VALUES (?,?,?,?,?)",
                      (_now(), event_id, title, event_time, bar_time))

    def log_trade(self, *, ticket: int | None, direction: str, volume: float, entry: float,
                  sl: float, tp: float, atr: float, risk_amount: float, equity: float,
                  status: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO trades(ts, ticket, direction, volume, entry, sl, tp, atr, risk_amount, equity, status, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), ticket, direction, volume, entry, sl, tp, atr, risk_amount, equity, status, detail))

    # ------------------------------------------------------------ state
    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._execute("SELECT value FROM system_state WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_state(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO system_state(key, value, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (key, json.dumps(value, default=str), _now()))

    def delete_state(self, key: str) -> None:
        self._execute("DELETE FROM system_state WHERE key = ?", (key,))

    def query(self, sql: str, params: tuple = ()) -> list:
        return self._execute(sql, params)
