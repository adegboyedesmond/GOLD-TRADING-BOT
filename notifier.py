"""
Telegram notifications. Messages are sent from a background thread so a slow
or failed Telegram request never delays trading. If TELEGRAM_BOT_TOKEN or
TELEGRAM_CHAT_ID is missing, notifications are silently disabled.
"""
from __future__ import annotations

import html
import logging
import queue
import threading

import config

log = logging.getLogger("notify")
API = "https://api.telegram.org/bot{token}/{method}"


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def _post(method: str, payload: dict | None = None, timeout: int = 10) -> dict:
    import requests
    resp = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                         json=payload or {}, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"HTTP {resp.status_code}"))
    return data


class Notifier:
    def __init__(self, sender=None):
        self.enabled = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
        self._send = sender or self._send_telegram
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="telegram", daemon=True)
            self._thread.start()

    def _send_telegram(self, text: str) -> None:
        _post("sendMessage", {"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                              "parse_mode": "HTML", "disable_web_page_preview": True})

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                break
            for attempt in (1, 2):
                try:
                    self._send(text)
                    break
                except Exception as exc:
                    if attempt == 2:
                        log.warning("Telegram send failed: %s", exc)
            self._queue.task_done()

    def send(self, text: str) -> None:
        """Queue a message (HTML allowed: <b>, <i>). Never raises."""
        if not self.enabled:
            return
        prefix = "🧪 <i>Practice mode</i>\n" if config.DRY_RUN else ""
        try:
            self._queue.put_nowait(prefix + text)
        except queue.Full:
            log.warning("Telegram queue full, message dropped")

    def flush(self, timeout: float = 8.0) -> None:
        """Wait briefly for queued messages (used on shutdown)."""
        if not self._thread:
            return
        done = threading.Event()

        def waiter():
            self._queue.join()
            done.set()
        threading.Thread(target=waiter, daemon=True).start()
        done.wait(timeout)


# ---------------------------------------------------------------- setup helpers
def print_chat_ids() -> None:
    """Shows the chat id of everyone who has messaged the bot recently."""
    if not config.TELEGRAM_BOT_TOKEN:
        print("Put TELEGRAM_BOT_TOKEN in .env first.")
        return
    updates = _post("getUpdates").get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start (or send 'hi'), then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


def send_test() -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return
    _post("sendMessage", {"chat_id": config.TELEGRAM_CHAT_ID, "parse_mode": "HTML",
                          "text": "✅ <b>Gold bot connected</b>\nYou will get trade alerts here."})
    print("Test message sent. Check Telegram.")
