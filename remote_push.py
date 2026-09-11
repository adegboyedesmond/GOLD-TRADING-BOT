"""Sends the dashboard payload to the cloud dashboard (Render) from a background
thread. Only the latest status is kept, so a slow network never builds a backlog."""
from __future__ import annotations

import logging
import threading
import time

import config

log = logging.getLogger("push")


class RemotePusher:
    def __init__(self, sender=None):
        self.enabled = bool(config.REMOTE_DASHBOARD_URL and config.DASHBOARD_PUSH_KEY)
        self.url = config.REMOTE_DASHBOARD_URL.rstrip("/") + "/api/push"
        self._send = sender or self._post
        self._latest = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._last_queued = float("-inf")
        self._last_error_log = 0.0
        self.last_ok = None
        if self.enabled:
            threading.Thread(target=self._worker, name="push", daemon=True).start()

    def _post(self, payload: dict) -> None:
        import requests
        resp = requests.post(self.url, json=payload, timeout=20,
                             headers={"X-Push-Key": config.DASHBOARD_PUSH_KEY})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")

    def due(self) -> bool:
        return self.enabled and time.monotonic() - self._last_queued >= config.REMOTE_PUSH_SECONDS

    def push(self, payload: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._latest = payload
        self._last_queued = time.monotonic()
        self._event.set()

    def _worker(self) -> None:
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                payload, self._latest = self._latest, None
            if payload is None:
                continue
            try:
                self._send(payload)
                if self.last_ok is None:
                    log.info("Cloud dashboard connected: %s", config.REMOTE_DASHBOARD_URL)
                self.last_ok = time.time()
            except Exception as exc:
                if time.time() - self._last_error_log > 300:     # log at most every 5 min
                    log.warning("Cloud dashboard push failed: %s", exc)
                    self._last_error_log = time.time()
