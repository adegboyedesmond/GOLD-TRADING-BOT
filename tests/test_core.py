"""Runs on any OS (no MetaTrader needed):  python -m pytest -q"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

import config
import strategy_engine as se
from backtest import run_backtest
from data_engine import DataEngine
from execution_bridge import Broker
from main import RuleBot
from news_filter import NewsFilter, parse_feed
from risk_manager import DailyBreaker, position_size, stop_levels

UTC = timezone.utc


def synthetic_m5(n=20_000, seed=1, start="2025-01-06"):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 0.8, n))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.6, n))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="5min", tz="UTC"),
        "open": open_, "close": close,
        "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread})


# ------------------------------------------------------------ strategy
def test_rsi_bounds():
    r = se.rsi(synthetic_m5(2000)["close"]).dropna()
    assert r.between(0, 100).all()


def test_decide_rules():
    buy, _ = se.decide(2010, 2000, 1.0, 1.1, 1.2, 1.1, 50, 3)
    sell, _ = se.decide(1990, 2000, 1.2, 1.1, 1.0, 1.1, 50, 3)
    assert (buy, sell) == ("BUY", "SELL")
    assert se.decide(2010, 2000, 1.0, 1.1, 1.2, 1.1, 65, 3)[0] is None     # RSI out of band
    assert se.decide(1990, 2000, 1.0, 1.1, 1.2, 1.1, 50, 3)[0] is None     # cross against trend
    assert se.decide(2010, 2000, 1.2, 1.1, 1.3, 1.1, 50, 3)[0] is None     # no fresh cross


def test_evaluate_needs_history():
    m5 = synthetic_m5(300)
    assert se.evaluate(m5, m5.iloc[:50]).direction is None


# ------------------------------------------------------------ risk
def test_stop_levels():
    assert stop_levels("BUY", 2000, 2.0) == (1997.0, 2006.0, 3.0)
    assert stop_levels("SELL", 2000, 2.0) == (2003.0, 1994.0, 3.0)


def test_position_size_one_percent():
    s = position_size(10_000, 4.5, 0.01, 1.0, 0.01, 100, 0.01)   # XAUUSD: $1 per 0.01 per lot
    assert s.volume == 0.22 and s.risk_amount <= 100


def test_position_size_never_rounds_up():
    assert position_size(100, 4.5, 0.01, 1.0, 0.01, 100, 0.01).volume == 0


def test_breaker(tmp_path):
    b = DailyBreaker(DataEngine(tmp_path / "t.db"))
    d = datetime(2026, 9, 10, 14, tzinfo=UTC)
    assert not b.update(d, 10_000, 10_000).tripped
    assert not b.update(d, 9_750, 9_750).tripped
    s = b.update(d, 9_700, 9_700)
    assert s.tripped and s.just_tripped
    assert b.update(d, 9_800, 9_800).tripped                        # stays locked
    assert not b.update(d + timedelta(days=1), 9_800, 9_800).tripped  # daily auto-reset


# ------------------------------------------------------------ news
FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-09-10T08:30:00-04:00", "impact": "High"},
    {"title": "ISM Services PMI", "country": "USD", "date": "2026-09-10T10:00:00-04:00", "impact": "High"},
    {"title": "CPI y/y", "country": "EUR", "date": "2026-09-10T05:00:00-04:00", "impact": "High"},
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-09-11T08:30:00-04:00", "impact": "High"},
]


def test_parse_feed_filters():
    titles = [e.title for e in parse_feed(FEED)]
    assert titles == ["CPI m/m", "Non-Farm Employment Change"]


def test_news_window(tmp_path):
    nf = NewsFilter(DataEngine(tmp_path / "t.db"), fetcher=lambda: FEED, cache_path=tmp_path / "c.json")
    release = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)     # 08:30 New York
    assert nf.check(release - timedelta(minutes=34)).locked  # 30 + 5 pre-check
    assert not nf.check(release - timedelta(minutes=36)).locked
    assert nf.check(release + timedelta(minutes=30)).locked
    assert not nf.check(release + timedelta(minutes=31)).locked


def test_news_fail_closed(tmp_path):
    def boom():
        raise ConnectionError("offline")
    nf = NewsFilter(DataEngine(tmp_path / "t.db"), fetcher=boom, cache_path=tmp_path / "c.json")
    assert nf.check(datetime.now(UTC)).locked


def test_clock_skew_blocks(tmp_path):
    wrong = datetime.now(UTC) + timedelta(hours=4)          # internet says 4 h later than PC
    nf = NewsFilter(DataEngine(tmp_path / "t.db"), fetcher=lambda: (FEED, wrong), cache_path=tmp_path / "c.json")
    status = nf.check(datetime.now(UTC))
    assert status.locked and "clock" in status.reason


def test_clock_ok(tmp_path):
    nf = NewsFilter(DataEngine(tmp_path / "t.db"), fetcher=lambda: (FEED, datetime.now(UTC)),
                    cache_path=tmp_path / "c.json")
    nf.check(datetime.now(UTC))
    assert nf.clock_ok()


# ------------------------------------------------------------ broker with fake MT5
class FakeMT5:
    TIMEFRAME_M5, TIMEFRAME_H1 = 5, 16385
    ACCOUNT_TRADE_MODE_DEMO = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 1, 0, 1
    ORDER_TIME_GTC, TRADE_RETCODE_DONE, POSITION_TYPE_BUY = 0, 10009, 0

    def __init__(self, trade_mode=0):
        self.sent, self.positions, self.trade_mode = [], [], trade_mode
        self.balance = 10_000.0
        self.m5 = synthetic_m5(400)
        self.h1 = synthetic_m5(1000, seed=2)

    def initialize(self, **kw): return True
    def shutdown(self): pass
    def last_error(self): return (1, "ok")
    def symbol_select(self, s, e): return True
    def terminal_info(self): return NS(trade_allowed=True)
    def account_info(self):
        return NS(login=1, server="Demo", currency="USD", trade_mode=self.trade_mode,
                  balance=self.balance, equity=self.balance)
    def symbol_info(self, s):
        return NS(digits=2, point=0.01, trade_stops_level=0, trade_tick_size=0.01, trade_tick_value=1.0,
                  trade_tick_value_loss=1.0, volume_min=0.01, volume_max=100, volume_step=0.01, filling_mode=1)
    def symbol_info_tick(self, s): return NS(bid=2000.0, ask=2000.3)
    def order_check(self, r): return NS(retcode=0, comment="ok")
    def order_send(self, r):
        self.sent.append(r)
        return NS(retcode=10009, order=555, price=r["price"], comment="done")
    def positions_get(self, symbol=None): return tuple(self.positions)
    def copy_rates_from_pos(self, sym, tf, start, count):
        df = (self.m5 if tf == self.TIMEFRAME_M5 else self.h1).tail(count).copy()
        df["time"] = df["time"].astype("int64") // 10**9
        return df.assign(tick_volume=1).to_records(index=False)


def test_bracket_order(tmp_path):
    fake = FakeMT5()
    broker = Broker(DataEngine(tmp_path / "t.db"), fake)
    assert broker.place_market_order("BUY", 3.0)
    r = fake.sent[0]
    assert r["volume"] == 0.22 and r["sl"] == 1995.8 and r["tp"] == 2009.3 and r["magic"] == config.MAGIC_NUMBER


def test_live_account_refused(tmp_path):
    broker = Broker(DataEngine(tmp_path / "t.db"), FakeMT5(trade_mode=2))
    with pytest.raises(Exception, match="Real-money"):
        broker.enforce_account_gate()


def test_close_all(tmp_path):
    fake = FakeMT5()
    fake.positions = [NS(ticket=9, symbol="XAUUSD", type=0, volume=0.1, magic=config.MAGIC_NUMBER,
                         price_open=1990, sl=1985, tp=2000),
                      NS(ticket=10, symbol="XAUUSD", type=0, volume=0.1, magic=1, price_open=1990, sl=0, tp=0)]
    broker = Broker(DataEngine(tmp_path / "t.db"), fake)
    assert broker.close_all_positions("test") == 0
    assert len(fake.sent) == 1 and fake.sent[0]["position"] == 9    # manual trade untouched


# ------------------------------------------------------------ main loop gates
def make_bot(tmp_path, feed=FEED):
    db = DataEngine(tmp_path / "t.db")
    fake = FakeMT5()
    news = NewsFilter(db, fetcher=lambda: feed, cache_path=tmp_path / "c.json")
    return RuleBot(db=db, broker=Broker(db, fake), news=news), fake


def test_gate_news_lock(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.step(datetime(2026, 9, 10, 12, 20, tzinfo=UTC)) == "NEWS_LOCK"
    assert bot.db.query("SELECT COUNT(*) FROM news_blocks")[0][0] == 1


def test_gate_session(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.step(datetime(2026, 9, 10, 9, 0, tzinfo=UTC)) == "OUT_OF_SESSION"


def test_gate_in_session_evaluates(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC)) in ("NO_SIGNAL", "ORDER")
    assert bot.step(datetime(2026, 9, 10, 15, 0, 5, tzinfo=UTC)) == "WAIT"   # same bar


def test_gate_breaker_kills_positions(tmp_path):
    bot, fake = make_bot(tmp_path)
    t = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    bot.step(t)
    fake.balance = 9_600
    fake.positions = [NS(ticket=9, symbol="XAUUSD", type=0, volume=0.1, magic=config.MAGIC_NUMBER,
                         price_open=1990, sl=1985, tp=2000)]
    assert bot.step(t) == "BREAKER"
    assert fake.sent[-1]["position"] == 9


# ------------------------------------------------------------ backtest
def test_backtest_runs():
    stats, trades = run_backtest(synthetic_m5(40_000))
    assert stats["trades"] == len(trades) > 0
    assert set(trades["outcome"]) <= {"SL", "TP"}


# ------------------------------------------------------------ dashboard
def test_status_published(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    s = bot.db.get_state("live_status")
    assert s["decision"]["action"] in ("WAIT", "BUY", "SELL")
    assert s["price"]["bid"] == 2000.0 and len(s["gates"]) == 7
    if s["plan"]:
        p = s["plan"]
        assert (p["sl"] > p["entry"] > p["tp"]) if p["direction"] == "SELL" else (p["tp"] > p["entry"] > p["sl"])


def test_status_news_lock(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.step(datetime(2026, 9, 10, 12, 20, tzinfo=UTC))
    s = bot.db.get_state("live_status")
    assert s["decision"]["action"] == "WAIT" and s["news"]["locked"]
    assert s["news"]["resume"].startswith("2026-09-10T13:00")


def test_dashboard_api(tmp_path):
    from dashboard import create_app
    bot, _ = make_bot(tmp_path)
    bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    client = create_app(bot.db).test_client()
    assert client.get("/").status_code == 200
    data = client.get("/api/status").get_json()
    assert data["live"]["symbol"] == config.SYMBOL


# ------------------------------------------------------------ telegram
class CaptureNotifier:
    def __init__(self): self.msgs = []
    def send(self, text): self.msgs.append(text)
    def flush(self, timeout=0): pass


def make_notified_bot(tmp_path, feed=FEED):
    db = DataEngine(tmp_path / "t.db")
    fake = FakeMT5()
    news = NewsFilter(db, fetcher=lambda: feed, cache_path=tmp_path / "c.json")
    cap = CaptureNotifier()
    return RuleBot(db=db, broker=Broker(db, fake), news=news, notifier=cap), fake, cap


def test_telegram_signal(tmp_path, monkeypatch):
    bot, fake, cap = make_notified_bot(tmp_path)
    monkeypatch.setattr(se, "evaluate", lambda m5, h1: se.Signal("SELL", 3.0, 2000.0,
                        {"trend": "DOWN", "ema_cross": "DOWN", "rsi": 45.0, "rsi_ok": True}))
    assert bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC)) == "ORDER"
    msg = cap.msgs[-1]
    assert "SELL" in msg and "2004.50" in msg and "1991.00" in msg and "ticket #555" in msg


def test_telegram_news_and_close(tmp_path):
    bot, fake, cap = make_notified_bot(tmp_path)
    bot.step(datetime(2026, 9, 10, 12, 20, tzinfo=UTC))
    assert any("News lockout" in m for m in cap.msgs)

    pos = NS(ticket=77, symbol="XAUUSD", type=1, volume=0.2, magic=config.MAGIC_NUMBER,
             price_open=2000, sl=2004.5, tp=1991, price_current=1995, profit=100)
    fake.positions = [pos]
    bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    fake.positions = []
    fake.history_deals_get = lambda position=None: (
        NS(entry=0, profit=0.0, commission=-1.0, swap=0.0, fee=0.0, volume=0.2, price=2000, reason=3),
        NS(entry=1, profit=180.0, commission=-1.0, swap=0.0, fee=0.0, volume=0.2, price=1991, reason=5))
    bot.step(datetime(2026, 9, 10, 15, 0, 5, tzinfo=UTC))
    assert any("Take profit hit" in m and "$178.00" in m for m in cap.msgs)


def test_notifier_disabled_without_token():
    from notifier import Notifier
    n = Notifier()
    assert not n.enabled
    n.send("x")        # no error, no network


def test_notifier_worker_sends(monkeypatch):
    import notifier
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    sent = []
    n = notifier.Notifier(sender=sent.append)
    n.send("hello")
    n.flush(2)
    assert sent and sent[0].endswith("hello")


# ------------------------------------------------------------ cloud dashboard
def test_cloud_push_roundtrip(tmp_path, monkeypatch):
    import dashboard
    from status_api import build_payload
    monkeypatch.setattr(dashboard, "PUSH_KEY", "secret")
    monkeypatch.setattr(dashboard, "PASSWORD", "pw")
    monkeypatch.setattr(dashboard, "CACHE_FILE", tmp_path / "latest.json")
    bot, _ = make_bot(tmp_path)
    bot.step(datetime(2026, 9, 10, 15, 0, tzinfo=UTC))
    payload = build_payload(bot.db)

    client = dashboard.create_app(cloud=True).test_client()
    assert client.post("/api/push", json=payload, headers={"X-Push-Key": "wrong"}).status_code == 403
    assert client.post("/api/push", json=payload, headers={"X-Push-Key": "secret"}).status_code == 200
    assert client.get("/api/status").status_code == 401                       # password required
    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}
    data = client.get("/api/status", headers=auth).get_json()
    assert data["live"]["symbol"] == config.SYMBOL
    assert client.get("/", headers=auth).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_pusher_sends_latest(monkeypatch):
    import remote_push
    monkeypatch.setattr(config, "REMOTE_DASHBOARD_URL", "https://x.onrender.com")
    monkeypatch.setattr(config, "DASHBOARD_PUSH_KEY", "k")
    sent = []
    p = remote_push.RemotePusher(sender=sent.append)
    assert p.due()
    p.push({"live": 1})
    import time as _t
    for _ in range(50):
        if sent:
            break
        _t.sleep(0.02)
    assert sent == [{"live": 1}] and not p.due()
