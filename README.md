# xau_rulebot

Rule-based XAUUSD trading bot for MetaTrader 5. It trades only when every rule passes, and it checks the news calendar before it looks at any chart signal.

## Rules (in the order the bot checks them)

| # | Gate | Rule |
|---|------|------|
| 0 | Daily breaker (every 5 s) | Closed P&L today <= -3% of the day's starting equity: close all bot positions and lock out |
| 1 | News lockout | USD high-impact CPI / Core PCE / NFP / FOMC within 35 min before to 30 min after the release (30 min window + 5 min pre-check): no entries, blocker ID logged |
| 2 | Session | Entries only 13:00-17:00 UTC (14:00-18:00 Lagos time) |
| 3 | Position cap | Max 1 open position |
| 4 | Trend | M5 close above H1 EMA200 = buys only, below = sells only |
| 5 | Trigger | M5 EMA9 crosses EMA21 in the trend direction on the last closed bar |
| 6 | Momentum | M5 RSI(14) between 40 and 60 |
| 7 | Order | Market order with SL = 1.5 x ATR(14) and TP = 2 x SL attached; size = 1% of equity |

If the news feed is down and there is no fresh cached data, the bot blocks entries (fail-closed).

## Files

```
config.py            all settings (risk, indicators, session, news keywords)
data_engine.py       SQLite: trades, signals, news_blocks, audit, system_state
news_filter.py       ForexFactory calendar -> lockout decision
strategy_engine.py   indicators + the rule checklist (shared with backtest)
risk_manager.py      lot sizing, SL/TP, daily drawdown breaker
execution_bridge.py  MetaTrader 5 connection, data, orders, close-all
main.py              main loop and CLI (also writes live status for the dashboard)
dashboard.py         web dashboard server (Flask)
notifier.py          Telegram alerts
web/dashboard.html   dashboard page
backtest.py          runs the same rules on M5 history
tests/               18 tests using a fake MT5 (work on any OS)
```

## Setup (Windows)

1. Install the MetaTrader 5 terminal from your broker, log in to a **demo** account and press **Algo Trading** so it turns green.
2. Install Python 3.11 (64-bit), then run:
   ```
   pip install -r requirements.txt
   copy .env.example .env
   ```
3. Edit `.env`. Set `SYMBOL` to your broker's gold symbol (`XAUUSD`, `XAUUSDm`, `GOLD`, ...).
4. Check the news feed: `python main.py --news`
5. Run the tests: `python -m pytest -q`

## Running

```
python main.py
```

Start with `DRY_RUN=true`. Orders are then logged instead of sent. Once the log looks right, set `DRY_RUN=false` and keep it on demo.

On a real-money account the bot refuses to trade unless `ALLOW_LIVE=true`.

If the daily breaker trips, it resets at 00:00 UTC. To make it stay locked until you clear it yourself, set `BREAKER_AUTO_RESET_DAILY = False` in `config.py` and reset with:

```
python main.py --reset-breaker
```

## Dashboard

Keep `python main.py` running. In a second PowerShell window:

```
python dashboard.py
```

Open http://127.0.0.1:5000 in your browser. It updates every 2 seconds and shows:

- The decision: **BUY**, **SELL**, **WAIT**, **IN TRADE** or **STOPPED**, with the reasons
- Entry, stop loss, take profit, lot size, and the dollar amounts you risk and could make
- Every rule with a tick or a cross
- Today's loss against the 3% limit, balance and equity
- Upcoming news that stops trading, in your local time
- Recent checks and orders

When the word says WAIT, the trade plan shows what a trade in the trend direction would look like at the current price. Only act on it when the word changes to BUY or SELL. The dashboard reads the bot's database only; it never sends orders.

## Telegram alerts

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the token it gives you.
2. Open your new bot in Telegram and press **Start**.
3. Add the token to `.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
4. Run `python main.py --telegram-chat-id` and copy the `TELEGRAM_CHAT_ID=...` line into `.env`.
5. Run `python main.py --test-telegram`.

You get a message when the bot starts or stops, the session opens or closes, a news lockout starts or ends, a BUY/SELL signal fires (with entry, stop loss, take profit, lots and dollar risk), a trade closes (with profit or loss), the daily loss limit trips, or the bot stops on errors.

## Cloud dashboard on Render (free)

The bot keeps running on your Windows PC (MT5 only runs on Windows). Render only hosts the dashboard; the bot pushes its status there every 10 seconds, so you can open it from your phone anywhere.

1. Upload this folder to a **private** GitHub repo. Never upload `.env` or `data/`.
2. Render: New, Web Service, pick the repo, then set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn dashboard:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`
   - Instance type: **Free**
   - Environment variables: `DASHBOARD_MODE=cloud`, `DASHBOARD_PUSH_KEY=<long random string>`, `DASHBOARD_PASSWORD=<your password>`
3. In the bot's `.env` add `REMOTE_DASHBOARD_URL=https://<your-service>.onrender.com` and the same `DASHBOARD_PUSH_KEY`, then restart the bot.

Make a random key with: `python -c "import secrets;print(secrets.token_urlsafe(32))"`

Render's free plan sleeps after 15 minutes without traffic. While the bot runs, its pushes keep it awake; when the bot is off, the page shows the bot as offline.

## Going live

Only after weeks of demo trading with `DRY_RUN=false`:

1. Open a real MT5 account with your broker and log in to it in MT5.
2. In `.env` set `DRY_RUN=false` and `ALLOW_LIVE=true`.
3. Check `SYMBOL` matches the broker's gold symbol.

With 1% risk and a typical 5-6 dollar stop, one 0.01 lot of gold risks about $5-6, so accounts below roughly $600 will have most trades skipped (the bot never rounds up past 1%).

## Backtest

```
python backtest.py --mt5 --days 365 --utc-offset 3
python backtest.py --csv xauusd_m5.csv --utc-offset 3
```

`--utc-offset` is your broker's server time minus UTC. Many gold brokers use +2 in winter and +3 in summer. The news lockout is **not** simulated, so live results will have fewer trades. Trades are saved to `data/backtest_trades.csv`.

At 2:1 reward to risk, the strategy breaks even at about a 34% win rate before costs. Judge it by profit factor and average R, not win rate alone.

## Where to look

- `data/rulebot.log`: console log
- `data/rulebot.db` (open with DB Browser for SQLite):
  - `trades`: every order, skip, rejection and close
  - `signals`: every in-session bar evaluation with all checks
  - `news_blocks`: which event ID blocked which bar
  - `audit`: lockouts, breaker trips, errors

## Known limits

- The ForexFactory feed is unofficial and covers the current week only.
- The breaker measures closed P&L from balance changes, so deposits or withdrawals during the day will distort it.
- The breaker only closes positions with this bot's magic number. Manual trades are left alone.
- SL and TP are set from the quoted price. Fills can slip by up to `MAX_SLIPPAGE_POINTS`.
