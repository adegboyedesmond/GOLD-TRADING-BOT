"""
Pre-trade news gatekeeper (spec 2B).

Pulls the ForexFactory weekly calendar, keeps only USD high-impact events whose
title matches config.NEWS_EVENT_KEYWORDS (CPI, Core PCE, NFP, FOMC), and reports
a lockout whenever "now" is inside:

    [release - WINDOW - PRECHECK,  release + WINDOW]

If fresh calendar data is unavailable the filter fails CLOSED (blocks entries)
unless config.NEWS_FAIL_CLOSED is set to False.
"""
from __future__ import annotations

import hashlib
import json
from email.utils import parsedate_to_datetime
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import config

log = logging.getLogger("news")


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    title: str
    time_utc: datetime


@dataclass
class NewsStatus:
    locked: bool
    reason: str
    blocking: list = field(default_factory=list)


def parse_feed(raw: list) -> list[NewsEvent]:
    """Convert raw ForexFactory JSON into the filtered list of blocker events."""
    keywords = [k.lower() for k in config.NEWS_EVENT_KEYWORDS]
    events: list[NewsEvent] = []
    for item in raw or []:
        try:
            if str(item.get("country", "")).upper() != config.NEWS_CURRENCY.upper():
                continue
            if str(item.get("impact", "")).lower() != config.NEWS_IMPACT.lower():
                continue
            title = str(item.get("title", "")).strip()
            if not any(k in title.lower() for k in keywords):
                continue
            t = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        event_id = "FF-" + hashlib.sha1(f"{title}|{t.isoformat()}".encode()).hexdigest()[:10]
        events.append(NewsEvent(event_id, title, t))
    events.sort(key=lambda e: e.time_utc)
    return events


def http_fetch():
    """Returns (calendar list, server UTC time from the HTTP Date header or None)."""
    import requests
    resp = requests.get(config.NEWS_FEED_URL, timeout=15,
                        headers={"User-Agent": "xau-rulebot/1.0"})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError("unexpected calendar format")
    server_time = None
    try:
        server_time = parsedate_to_datetime(resp.headers["Date"]).astimezone(timezone.utc)
    except Exception:
        pass
    return data, server_time


class NewsFilter:
    RETRY_AFTER = timedelta(minutes=5)

    def __init__(self, db, fetcher: Optional[Callable[[], list]] = None,
                 cache_path: Path | str = config.NEWS_CACHE_PATH):
        self.db = db
        self.fetch = fetcher or http_fetch
        self.cache_path = Path(cache_path)
        self.events: list[NewsEvent] = []
        self.data_time: Optional[datetime] = None
        self.last_attempt: Optional[datetime] = None
        self.clock_skew: Optional[float] = None   # seconds, PC clock minus internet time
        self._load_cache()

    # ------------------------------------------------------------ cache
    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.events = parse_feed(payload["raw"])
            self.data_time = datetime.fromisoformat(payload["fetched_at"])
        except FileNotFoundError:
            pass
        except Exception as exc:  # corrupt cache is not fatal
            log.warning("Ignoring unreadable news cache: %s", exc)

    def _save_cache(self, raw: list, now: datetime) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": now.isoformat(), "raw": raw}),
                                       encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write news cache: %s", exc)

    # ------------------------------------------------------------ refresh
    def refresh(self, now: datetime, force: bool = False) -> None:
        due = force or self.data_time is None or \
            now - self.data_time >= timedelta(minutes=config.NEWS_REFRESH_MINUTES)
        if not due:
            return
        if not force and self.last_attempt and now - self.last_attempt < self.RETRY_AFTER:
            return
        self.last_attempt = now
        try:
            result = self.fetch()
            raw, server_time = result if isinstance(result, tuple) else (result, None)
            events = parse_feed(raw)
        except Exception as exc:
            log.warning("News feed fetch failed: %s", exc)
            self.db.audit("WARN", f"News feed fetch failed: {exc}")
            return
        if server_time is not None:
            self.clock_skew = (datetime.now(timezone.utc) - server_time).total_seconds()
            if not self.clock_ok():
                log.critical("PC clock is off by %.0f seconds vs internet time", self.clock_skew)
                self.db.audit("CRITICAL", f"PC clock off by {self.clock_skew:.0f}s")
        self.events = events
        self.data_time = now
        self._save_cache(raw, now)
        log.info("News feed refreshed: %d blocker events this week", len(events))

    def clock_ok(self) -> bool:
        return self.clock_skew is None or abs(self.clock_skew) <= config.CLOCK_MAX_SKEW_SECONDS

    def is_fresh(self, now: datetime) -> bool:
        return self.data_time is not None and \
            now - self.data_time <= timedelta(hours=config.NEWS_MAX_CACHE_AGE_HOURS)

    # ------------------------------------------------------------ gate
    def check(self, now: datetime) -> NewsStatus:
        self.refresh(now)
        if not self.clock_ok():
            return NewsStatus(True, f"PC clock wrong by {self.clock_skew:.0f}s - fix Windows time zone/sync")
        if not self.is_fresh(now):
            if config.NEWS_FAIL_CLOSED:
                return NewsStatus(True, "news feed unavailable or stale (fail-closed)")
            return NewsStatus(False, "news feed unavailable (fail-open)")

        before = timedelta(minutes=config.NEWS_WINDOW_MINUTES + config.NEWS_PRECHECK_MINUTES)
        after = timedelta(minutes=config.NEWS_WINDOW_MINUTES)
        blocking = [e for e in self.events if e.time_utc - before <= now <= e.time_utc + after]
        if blocking:
            names = ", ".join(f"{e.title} @ {e.time_utc:%H:%M} UTC [{e.event_id}]" for e in blocking)
            return NewsStatus(True, f"high-impact news window: {names}", blocking)
        return NewsStatus(False, "clear")

    def upcoming(self, now: datetime, hours: int = 48) -> list[NewsEvent]:
        horizon = now + timedelta(hours=hours)
        return [e for e in self.events if now - timedelta(minutes=config.NEWS_WINDOW_MINUTES)
                <= e.time_utc <= horizon]
