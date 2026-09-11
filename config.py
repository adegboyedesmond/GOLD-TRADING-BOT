"""
Central configuration for xau_rulebot.

Secrets (MT5 login etc.) are read from environment variables or a local .env
file. Never hard-code credentials in this file.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv is optional
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- broker / MT5
MT5_LOGIN = int(_env("MT5_LOGIN", "0") or 0)       # 0 = use terminal's logged-in account
MT5_PASSWORD = _env("MT5_PASSWORD")
MT5_SERVER = _env("MT5_SERVER")
MT5_PATH = _env("MT5_PATH")                        # optional path to terminal64.exe
SYMBOL = _env("SYMBOL", "XAUUSD")                  # some brokers use XAUUSDm, GOLD, etc.
MAGIC_NUMBER = 20260911                            # tags this bot's orders/positions
ORDER_COMMENT = "xau_rulebot"                      # MT5 limit: 31 chars
MAX_SLIPPAGE_POINTS = 30

# ---------------------------------------------------------------- safety gates
ALLOW_LIVE = _bool("ALLOW_LIVE", False)   # refuse to trade real-money accounts unless true
DRY_RUN = _bool("DRY_RUN", False)         # log orders instead of sending them

# ---------------------------------------------------------------- strategy (spec 2A)
H1_TREND_EMA = 200
M5_FAST_EMA = 9
M5_SLOW_EMA = 21
RSI_PERIOD = 14
RSI_MIN = 40.0
RSI_MAX = 60.0
ATR_PERIOD = 14
H1_BARS = 1000      # history pulled for the 200 EMA (extra bars = EMA warm-up)
M5_BARS = 400

# ---------------------------------------------------------------- risk (spec 2D)
RISK_PER_TRADE = 0.01          # 1.0% of equity per trade
SL_ATR_MULTIPLIER = 1.5        # SL = 1.5 x ATR(14) on M5
RISK_REWARD = 2.0              # TP = 2 x SL distance
DAILY_MAX_LOSS = 0.03          # 3% of starting daily equity -> kill switch
BREAKER_AUTO_RESET_DAILY = True  # False = stay locked until `python main.py --reset-breaker`
MAX_OPEN_POSITIONS = 1

# ---------------------------------------------------------------- session (spec 2C), UTC hours
SESSION_START_UTC = 13         # inclusive 13:00
SESSION_END_UTC = 17           # exclusive 17:00

# ---------------------------------------------------------------- news gatekeeper (spec 2B)
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"  # ForexFactory weekly feed
NEWS_REFRESH_MINUTES = 60      # the feed is rate-limited; hourly is plenty
NEWS_MAX_CACHE_AGE_HOURS = 24  # older data counts as "no data"
NEWS_WINDOW_MINUTES = 30       # lockout 30 min before and after the release
NEWS_PRECHECK_MINUTES = 5      # extra look-ahead before evaluating a signal
NEWS_CURRENCY = "USD"
NEWS_IMPACT = "High"
NEWS_EVENT_KEYWORDS = [        # matched case-insensitively against ForexFactory titles
    "cpi",                          # CPI m/m, Core CPI m/m, CPI y/y
    "core pce",                     # Core PCE Price Index m/m
    "non-farm employment change",   # ForexFactory's name for NFP
    "non-farm payrolls",
    "federal funds rate",           # FOMC rate decision
    "fomc statement",
    "fomc press conference",
    "fomc economic projections",
]
NEWS_FAIL_CLOSED = True        # no fresh news data -> block entries
CLOCK_MAX_SKEW_SECONDS = 120   # PC clock vs internet time; beyond this the bot refuses to trade

# ---------------------------------------------------------------- Telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
TELEGRAM_SESSION_ALERTS = True   # message when the 13:00-17:00 UTC window opens/closes

# ---------------------------------------------------------------- cloud dashboard (Render)
REMOTE_DASHBOARD_URL = _env("REMOTE_DASHBOARD_URL")   # e.g. https://gold-bot-dashboard.onrender.com
DASHBOARD_PUSH_KEY = _env("DASHBOARD_PUSH_KEY")
REMOTE_PUSH_SECONDS = 10

# ---------------------------------------------------------------- main loop
POLL_SECONDS = 5
MAX_CONSECUTIVE_ERRORS = 20

# ---------------------------------------------------------------- storage
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "rulebot.db"
NEWS_CACHE_PATH = DATA_DIR / "news_cache.json"
LOG_PATH = DATA_DIR / "rulebot.log"
