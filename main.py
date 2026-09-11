"""
xau_rulebot main loop.

Every poll (5 s):   daily drawdown breaker, live status for the dashboard
Every new M5 bar:   1) news lockout  2) session window  3) position cap
                    4) technical checklist  ->  bracket order

Usage:
    python main.py                  run the bot
    python main.py --news           show this week's news blockers and exit
    python main.py --reset-breaker  manually clear a tripped daily breaker
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import config
import strategy_engine
from data_engine import DataEngine
from execution_bridge import Broker, BrokerError
from news_filter import NewsFilter, NewsStatus
from notifier import Notifier, esc, print_chat_ids, send_test
from remote_push import RemotePusher
from status_api import build_payload
from risk_manager import DailyBreaker

log = logging.getLogger("main")
STATUS_KEY = "live_status"


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOG_PATH, encoding="utf-8")])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def in_session(now: datetime) -> bool:
    return config.SESSION_START_UTC <= now.hour < config.SESSION_END_UTC


def session_info(now: datetime) -> dict:
    day = now.replace(minute=0, second=0, microsecond=0)
    opens = day.replace(hour=config.SESSION_START_UTC)
    closes = day.replace(hour=config.SESSION_END_UTC)
    is_open = opens <= now < closes and now.weekday() < 5
    if is_open:
        return {"open": True, "closes": closes.isoformat(), "next_open": None}
    nxt = opens if now < opens else opens + timedelta(days=1)
    while nxt.weekday() >= 5:                       # gold is closed at weekends
        nxt += timedelta(days=1)
    return {"open": False, "closes": None, "next_open": nxt.isoformat()}


def local(t: datetime) -> str:
    """UTC datetime -> PC local time (Lagos once Windows is set to UTC+1)."""
    return t.astimezone().strftime("%H:%M")


def usd(x: float) -> str:
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def technical_reasons(checks: dict) -> list[str]:
    if "error" in checks:
        return [checks["error"]]
    trend = checks["trend"]
    if trend == "FLAT":
        return ["Price is sitting exactly on the 1-hour 200 EMA, so there is no trend direction."]
    up = trend == "UP"
    reasons = [f"Trend is {'up' if up else 'down'}: price is {'above' if up else 'below'} the 1-hour 200 EMA, "
               f"so only {'buys' if up else 'sells'} are allowed."]
    if checks["ema_cross"] != trend:
        reasons.append(f"Waiting for the 9 EMA to cross {'above' if up else 'below'} the 21 EMA on a 5-minute candle.")
    if not checks["rsi_ok"]:
        reasons.append(f"RSI is {checks['rsi']}, outside the {config.RSI_MIN:g}-{config.RSI_MAX:g} range.")
    return reasons


class RuleBot:
    def __init__(self, db: DataEngine | None = None, broker: Broker | None = None,
                 news: NewsFilter | None = None, notifier: Notifier | None = None):
        self.db = db or DataEngine()
        self.notify = notifier or Notifier()
        self.pusher = RemotePusher()
        self.news = news or NewsFilter(self.db)
        self.breaker = DailyBreaker(self.db)
        self.broker = broker or Broker(self.db)
        self.last_bar_time = None
        self.news_locked = False
        self.running = True
        self.analysis = None          # latest strategy_engine.Signal
        self.analysis_bar = None
        self.signal_bar = None        # bar on which all rules + gates passed
        self.signal_time = None
        self.known_tickets = None     # positions seen on the previous poll
        self.pending_closed = {}      # ticket -> polls waited for deal history
        self.session_was_open = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.broker.connect()
        self.broker.enforce_account_gate()
        acct = self.broker.account()
        mode = "DEMO" if self.broker.is_demo() else "LIVE"
        msg = (f"Started on {mode} account {acct.login} ({acct.server}), equity {acct.equity:.2f} "
               f"{acct.currency}, dry_run={config.DRY_RUN}")
        log.info(msg)
        self.db.audit("INFO", msg)
        now = utcnow()
        self.news.refresh(now, force=True)
        if self.news.clock_skew is None:
            log.warning("Could not verify PC clock against internet time")
        elif not self.news.clock_ok():
            raise BrokerError(f"PC clock is off by {self.news.clock_skew:.0f} seconds. "
                              "Set the correct Windows time zone, sync the clock, then restart.")
        else:
            log.info("PC clock verified (off by %.0f s)", self.news.clock_skew)
        upcoming = self.news.upcoming(now)
        for ev in upcoming:
            log.info("Upcoming news blocker: %s at %s UTC [%s]", ev.title, f"{ev.time_utc:%a %H:%M}", ev.event_id)
        news_lines = "".join(f"\n  {ev.time_utc.astimezone():%a %H:%M}  {esc(ev.title)}" for ev in upcoming[:4])
        self.notify.send(
            f"🤖 <b>Gold bot started</b>\n"
            f"Account {acct.login} ({esc(acct.server)}), <b>{mode}</b>\n"
            f"Equity {usd(acct.equity)}\n"
            f"Orders: {'practice only, nothing is sent' if config.DRY_RUN else 'REAL orders are sent to MT5'}"
            + (f"\n\nNews that will pause trading:{news_lines}" if news_lines else ""))

    def stop(self) -> None:
        log.info("Shutting down")
        self.db.audit("INFO", "Bot stopped")
        self.notify.send("⏹ <b>Gold bot stopped</b>. No new trades until it is restarted.")
        self.notify.flush()
        try:
            self.broker.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------ one cycle
    def step(self, now: datetime | None = None) -> str:
        now = now or utcnow()
        self.broker.ensure_connected()
        acct = self.broker.account()
        news_status = self.news.check(now)

        # Circuit breaker: independent of every other rule
        b = self.breaker.update(now, acct.balance, acct.equity)
        if b.tripped:
            if b.just_tripped:
                msg = (f"DAILY DRAWDOWN BREAKER TRIPPED: closed P&L {b.realized_pnl:.2f} <= "
                       f"-{b.loss_limit:.2f} (3% of {b.start_equity:.2f}). Closing all positions.")
                log.critical(msg)
                self.db.audit("CRITICAL", msg)
                self.notify.send(f"🛑 <b>Daily loss limit hit</b>\nClosed P&amp;L today {usd(b.realized_pnl)} "
                                 f"(limit {usd(-b.loss_limit)}).\nAll bot trades closed. No more trades today.")
            if self.broker.open_positions():
                self.broker.close_all_positions("daily_dd_breaker")
            elif not config.BREAKER_AUTO_RESET_DAILY:
                self.running = False          # disable main loop until manual reset
            self._publish(now, acct, b, news_status)
            return "BREAKER"

        self._track_positions()
        self._track_session(now)

        m5 = self.broker.closed_bars("M5", config.M5_BARS)
        bar_time = m5["time"].iloc[-1]
        result = "WAIT"
        if bar_time != self.last_bar_time:
            self.last_bar_time = bar_time
            result = self._on_new_bar(now, str(bar_time), m5, news_status)
        self._publish(now, acct, b, news_status)
        return result

    def _on_new_bar(self, now: datetime, bar_str: str, m5, status: NewsStatus) -> str:
        # Analysis is computed every bar for the dashboard; trading still obeys the gate order.
        h1 = self.broker.closed_bars("H1", config.H1_BARS)
        self.analysis = strategy_engine.evaluate(m5, h1)
        self.analysis_bar = bar_str

        # Gate 1: news lockout
        if status.locked:
            if not self.news_locked:
                log.warning("NEWS LOCKOUT ON: %s", status.reason)
                self.db.audit("WARN", f"News lockout on: {status.reason}")
                if status.blocking:
                    until = max(e.time_utc for e in status.blocking) + timedelta(minutes=config.NEWS_WINDOW_MINUTES)
                    names = "".join(f"\n  {local(e.time_utc)}  {esc(e.title)}" for e in status.blocking)
                    self.notify.send(f"📰 <b>News lockout</b>. No new trades until {local(until)}.{names}")
                else:
                    self.notify.send(f"⚠️ <b>Trading paused</b>: {esc(status.reason)}")
            self.news_locked = True
            for ev in status.blocking:
                self.db.log_news_block(ev.event_id, ev.title, ev.time_utc.isoformat(), bar_str)
            if not status.blocking:
                self.db.log_news_block("FEED-UNAVAILABLE", status.reason, "", bar_str)
            return "NEWS_LOCK"
        if self.news_locked:
            log.info("NEWS LOCKOUT OFF")
            self.db.audit("INFO", "News lockout lifted")
            self.notify.send("✅ <b>News lockout over</b>. The bot is checking the rules again.")
            self.news_locked = False

        # Gate 2: London/New York overlap only
        if not in_session(now):
            return "OUT_OF_SESSION"

        # Gate 3: one position at a time
        if len(self.broker.open_positions()) >= config.MAX_OPEN_POSITIONS:
            return "POSITION_OPEN"

        # Gate 4: technical checklist
        sig = self.analysis
        if sig.direction is None:
            self.db.log_signal(bar_str, None, "NO_SIGNAL", sig.checks)
            return "NO_SIGNAL"

        log.info("SIGNAL %s %s", sig.direction, sig.checks)
        self.signal_bar = bar_str
        self.signal_time = now
        placed = self.broker.place_market_order(sig.direction, sig.atr)
        self.db.log_signal(bar_str, sig.direction, "ORDER_PLACED" if placed else "ORDER_NOT_PLACED", sig.checks)
        self._notify_order(sig, placed)
        return "ORDER" if placed else "ORDER_NOT_PLACED"

    # ------------------------------------------------------------ notifications
    def _notify_order(self, sig, res) -> None:
        p = res.plan
        d = p.get("digits", 2)
        icon = "🟩" if sig.direction == "BUY" else "🟥"
        status = {"OPEN": f"Order filled, ticket #{res.ticket}",
                  "DRY_RUN": "Practice mode: logged, not sent"}.get(res.status, f"Not placed: {esc(res.detail)}")
        self.notify.send(
            f"{icon} <b>{sig.direction} {config.SYMBOL}</b>\n"
            f"Entry <b>{res.entry:.{d}f}</b>\n"
            f"Stop loss <b>{p['sl']:.{d}f}</b>\n"
            f"Take profit <b>{p['tp']:.{d}f}</b>\n"
            f"Lots {p['volume']}, risk {usd(p['risk_amount'])}, target {usd(p['reward_amount'])}\n"
            f"RSI {sig.checks.get('rsi')}, ATR {sig.atr:.{d}f}\n"
            f"{status}")

    def _track_positions(self) -> None:
        """Detect positions that closed since the last poll and report the result."""
        try:
            tickets = {p.ticket for p in self.broker.open_positions()}
            if self.known_tickets is not None:
                for t in self.known_tickets - tickets:
                    self.pending_closed[t] = 0
            self.known_tickets = tickets
            for t in list(self.pending_closed):
                res = self.broker.position_result(t)
                if res:
                    win = res["profit"] >= 0
                    msg = f"Trade #{t} closed: {res['reason']}, profit {usd(res['profit'])}"
                    log.info(msg)
                    self.db.audit("INFO", msg)
                    self.notify.send(f"{'✅' if win else '❌'} <b>{res['reason']}</b>, trade #{t}\n"
                                     f"Closed at {res['close_price']}\n"
                                     f"{'Profit' if win else 'Loss'} <b>{usd(res['profit'])}</b>")
                    del self.pending_closed[t]
                else:
                    self.pending_closed[t] += 1
                    if self.pending_closed[t] >= 24:          # ~2 minutes without history
                        self.notify.send(f"ℹ️ Trade #{t} closed. Check MT5 History for the result.")
                        del self.pending_closed[t]
        except BrokerError:
            raise
        except Exception as exc:
            log.warning("Position tracking failed: %s", exc)

    def _track_session(self, now: datetime) -> None:
        is_open = session_info(now)["open"]
        if self.session_was_open is not None and is_open != self.session_was_open and config.TELEGRAM_SESSION_ALERTS:
            if is_open:
                close = now.replace(hour=config.SESSION_END_UTC, minute=0, second=0, microsecond=0)
                self.notify.send(f"🔔 <b>Trading session open</b> until {local(close)}. Watching for signals.")
            else:
                self.notify.send("🌙 <b>Trading session closed</b>. No new trades until tomorrow.")
        self.session_was_open = is_open

    # ------------------------------------------------------------ dashboard status
    def _publish(self, now: datetime, acct, b, news: NewsStatus) -> None:
        try:
            self.db.set_state(STATUS_KEY, self._build_status(now, acct, b, news))
            if self.pusher.due():
                self.pusher.push(build_payload(self.db))
        except Exception as exc:              # the dashboard must never stop the bot
            log.debug("Status publish failed: %s", exc)

    def _build_status(self, now: datetime, acct, b, news: NewsStatus) -> dict:
        tick = self.broker.tick()
        positions = self.broker.open_positions()
        session = session_info(now)
        sig = self.analysis
        checks = sig.checks if sig else {}

        resume = None
        if news.locked and news.blocking:
            resume = max(e.time_utc for e in news.blocking) + timedelta(minutes=config.NEWS_WINDOW_MINUTES)
        upcoming = [{"title": e.title, "time": e.time_utc.isoformat(), "id": e.event_id}
                    for e in self.news.upcoming(now, hours=24 * 7)][:6]

        # ---- rule checklist in the order the bot applies it
        trend = checks.get("trend")
        gate_list = [
            {"name": "Daily loss limit", "ok": not b.tripped,
             "detail": f"Closed P&L today {b.realized_pnl:+.2f} of -{b.loss_limit:.2f} allowed"},
            {"name": "News", "ok": not news.locked,
             "detail": "No high-impact US news nearby" if not news.locked else news.reason},
            {"name": "Trading hours", "ok": session["open"],
             "detail": "London/New York overlap is open" if session["open"] else "Outside 13:00-17:00 UTC"},
            {"name": "Open positions", "ok": len(positions) < config.MAX_OPEN_POSITIONS,
             "detail": f"{len(positions)} of {config.MAX_OPEN_POSITIONS} used"},
        ]
        if checks and "error" not in checks:
            gate_list += [
                {"name": "1-hour trend", "ok": trend in ("UP", "DOWN"),
                 "detail": f"Price {checks['close']} vs 200 EMA {checks['h1_ema200']}: "
                           f"{'up, buys only' if trend == 'UP' else 'down, sells only' if trend == 'DOWN' else 'flat'}"},
                {"name": "EMA cross", "ok": checks["ema_cross"] is not None and checks["ema_cross"] == trend,
                 "detail": f"9 EMA {checks['ema_fast']} vs 21 EMA {checks['ema_slow']}, "
                           f"{'crossed ' + checks['ema_cross'].lower() if checks['ema_cross'] else 'no fresh cross'}"},
                {"name": "RSI 40-60", "ok": checks["rsi_ok"], "detail": f"RSI {checks['rsi']}"},
            ]

        # ---- the decision shown in big letters
        if b.tripped:
            action, headline = "STOPPED", "Daily loss limit hit. No more trades today."
            reasons = [f"Closed P&L today is {b.realized_pnl:+.2f}, limit is -{b.loss_limit:.2f}."]
        elif positions:
            action, headline = "IN TRADE", "A trade is open. Let the stop loss or take profit close it."
            reasons = []
        elif news.locked:
            action = "WAIT"
            headline = "News lockout. Do not trade."
            reasons = [news.reason] + ([f"Trading resumes at {resume.isoformat()}"] if resume else [])
        elif not session["open"]:
            action, headline = "WAIT", "Outside trading hours."
            reasons = [f"The session opens at {session['next_open']}"] + (technical_reasons(checks) if checks else [])
        elif sig and sig.direction and self.signal_bar == self.analysis_bar:
            action = sig.direction
            headline = f"All rules passed. {'Buy' if action == 'BUY' else 'Sell'} gold now."
            reasons = [f"Signal found at {self.signal_time.isoformat()}. It is valid until the next 5-minute candle closes."]
        else:
            action, headline = "WAIT", "No trade yet. Rules not all met."
            reasons = technical_reasons(checks) if checks else ["Waiting for the first closed candle."]

        # ---- trade plan: the live signal, or what a trade in the trend direction would look like now
        plan, plan_label = None, None
        plan_dir = action if action in ("BUY", "SELL") else ("BUY" if trend == "UP" else "SELL" if trend == "DOWN" else None)
        if plan_dir and sig and sig.atr == sig.atr and not positions and not b.tripped:
            plan = self.broker.preview_plan(plan_dir, sig.atr)
            plan.pop("_filling", None)
            plan_label = "signal" if action in ("BUY", "SELL") else "preview"

        pos_list = [{"ticket": p.ticket, "type": "BUY" if p.type == 0 else "SELL", "volume": p.volume,
                     "entry": p.price_open, "sl": p.sl, "tp": p.tp,
                     "price": getattr(p, "price_current", None), "profit": getattr(p, "profit", None)}
                    for p in positions]

        return {
            "updated": now.isoformat(),
            "symbol": config.SYMBOL,
            "dry_run": config.DRY_RUN,
            "account": {"login": acct.login, "server": acct.server, "currency": acct.currency,
                        "demo": acct.trade_mode == self.broker.mt5.ACCOUNT_TRADE_MODE_DEMO,
                        "balance": acct.balance, "equity": acct.equity},
            "price": {"bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 2)},
            "decision": {"action": action, "headline": headline, "reasons": reasons},
            "plan": plan, "plan_label": plan_label,
            "gates": gate_list,
            "analysis_bar": self.analysis_bar,
            "breaker": {"tripped": b.tripped, "pnl": round(b.realized_pnl, 2), "limit": round(b.loss_limit, 2),
                        "start_equity": b.start_equity},
            "session": session,
            "news": {"locked": news.locked, "reason": news.reason,
                     "resume": resume.isoformat() if resume else None, "upcoming": upcoming},
            "positions": pos_list,
            "settings": {"risk_pct": config.RISK_PER_TRADE * 100, "rr": config.RISK_REWARD,
                         "sl_atr": config.SL_ATR_MULTIPLIER},
        }

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        self.start()
        errors = 0
        while self.running:
            try:
                self.step()
                errors = 0
            except BrokerError as exc:
                errors += 1
                log.error("Broker error: %s", exc)
                self.db.audit("ERROR", f"Broker error: {exc}")
                try:
                    self.broker.shutdown()
                    time.sleep(2)
                    self.broker.connect()
                except Exception as exc2:
                    log.error("Reconnect failed: %s", exc2)
            except Exception as exc:
                errors += 1
                log.exception("Unexpected error")
                self.db.audit("ERROR", f"Unexpected error: {exc!r}")
            if errors >= config.MAX_CONSECUTIVE_ERRORS:
                log.critical("Too many consecutive errors, stopping.")
                self.db.audit("CRITICAL", "Stopped after too many consecutive errors")
                self.notify.send("🚨 <b>Bot stopped after repeated errors.</b> Check the PowerShell window and MT5.")
                break
            if self.running:
                time.sleep(config.POLL_SECONDS)
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD rule-based trading bot (MT5)")
    parser.add_argument("--reset-breaker", action="store_true", help="clear a tripped daily breaker")
    parser.add_argument("--news", action="store_true", help="show news blockers and exit")
    parser.add_argument("--telegram-chat-id", action="store_true", help="show your Telegram chat id")
    parser.add_argument("--test-telegram", action="store_true", help="send a Telegram test message")
    args = parser.parse_args()
    setup_logging()

    if args.telegram_chat_id:
        print_chat_ids()
        return
    if args.test_telegram:
        send_test()
        return

    if args.reset_breaker:
        DailyBreaker(DataEngine()).manual_reset()
        print("Daily breaker cleared.")
        return

    if args.news:
        nf = NewsFilter(DataEngine())
        now = utcnow()
        nf.refresh(now, force=True)
        print(f"Now {now:%a %H:%M} UTC -> {nf.check(now).reason}")
        if nf.clock_skew is not None:
            print(f"PC clock vs internet: {nf.clock_skew:+.0f} s ({'OK' if nf.clock_ok() else 'WRONG'})")
        for ev in nf.events:
            print(f"  {ev.time_utc:%a %d %b %H:%M} UTC  {ev.title}  [{ev.event_id}]")
        return

    bot = RuleBot()

    def _stop(*_):
        log.info("Stop requested")
        bot.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        bot.run()
    except BrokerError as exc:
        log.critical("%s", exc)
        bot.notify.send(f"🚨 <b>Bot could not start</b>\n{esc(exc)}")
        bot.notify.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
