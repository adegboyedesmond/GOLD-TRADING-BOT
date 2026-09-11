"""
Backtest the exact live rules on M5 history.

    python backtest.py --csv xauusd_m5.csv --utc-offset 3
    python backtest.py --mt5 --days 365 --utc-offset 3     (Windows + MT5 only)

CSV needs columns: time, open, high, low, close   (bid prices, one row per M5 bar)
--utc-offset = your broker's server time offset from UTC (often 2 or 3).
H1 bars are built from the M5 data. Entries fill at the next bar's open.
If SL and TP are both touched in one bar, the SL is assumed (conservative).

NOT simulated: the news lockout (no historical calendar), slippage, commission.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import config
import strategy_engine as se
from risk_manager import stop_levels


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = {"time", "open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise SystemExit(f"CSV missing columns: {missing}")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df[["time", "open", "high", "low", "close"]]


def load_mt5(days: int) -> pd.DataFrame:
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(config.SYMBOL, True)
        rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M5, 1, days * 288)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise SystemExit("No history returned. Raise 'Max bars in chart' in MT5 options.")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time", "open", "high", "low", "close"]]


def prepare(m5: pd.DataFrame, utc_offset_hours: float) -> pd.DataFrame:
    df = m5.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    df["time"] = df["time"] - pd.Timedelta(hours=utc_offset_hours)   # server time -> UTC
    df = se.add_indicators(df)

    h1 = (df.set_index("time")[["open", "high", "low", "close"]]
          .resample("1h", label="left", closed="left")
          .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    h1["h1_ema"] = se.ema(h1["close"], config.H1_TREND_EMA)
    h1["avail"] = h1.index + pd.Timedelta(hours=1)          # usable once the H1 bar has closed
    h1 = h1.iloc[config.H1_TREND_EMA:]                       # EMA warm-up

    df["close_time"] = df["time"] + pd.Timedelta(minutes=5)
    df = pd.merge_asof(df, h1[["avail", "h1_ema"]].reset_index(drop=True),
                       left_on="close_time", right_on="avail", direction="backward")
    return df


def run_backtest(m5: pd.DataFrame, utc_offset_hours: float = 0.0, spread: float = 0.30,
                 start_equity: float = 10_000.0) -> tuple[dict, pd.DataFrame]:
    df = prepare(m5, utc_offset_hours)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    fast, slow = df["ema_fast"].to_numpy(float), df["ema_slow"].to_numpy(float)
    rsi_v, atr_v, h1e = df["rsi"].to_numpy(float), df["atr"].to_numpy(float), df["h1_ema"].to_numpy(float)
    close_time = df["close_time"]
    hours = close_time.dt.hour.to_numpy()
    days = close_time.dt.date.to_numpy()

    equity = peak = start_equity
    max_dd = 0.0
    trades, pos = [], None
    day, day_start, day_pnl, day_locked, breaker_days = None, equity, 0.0, False, 0

    for i in range(1, len(df) - 1):
        if days[i] != day:
            day, day_start, day_pnl, day_locked = days[i], equity, 0.0, False

        # manage open position on this bar
        if pos is not None and i >= pos["entry_i"]:
            exit_px = outcome = None
            if pos["dir"] == "BUY":            # long exits on bid
                if l[i] <= pos["sl"]:
                    exit_px, outcome = pos["sl"], "SL"
                elif h[i] >= pos["tp"]:
                    exit_px, outcome = pos["tp"], "TP"
            else:                              # short exits on ask = bid + spread
                if h[i] + spread >= pos["sl"]:
                    exit_px, outcome = pos["sl"], "SL"
                elif l[i] + spread <= pos["tp"]:
                    exit_px, outcome = pos["tp"], "TP"
            if exit_px is not None:
                move = exit_px - pos["entry"] if pos["dir"] == "BUY" else pos["entry"] - exit_px
                r = move / pos["dist"]
                pnl = r * pos["risk"]
                equity += pnl
                day_pnl += pnl
                trades.append({"entry_time": pos["time"], "exit_time": close_time.iloc[i],
                               "dir": pos["dir"], "entry": pos["entry"], "sl": pos["sl"],
                               "tp": pos["tp"], "exit": exit_px, "outcome": outcome,
                               "r": r, "pnl": pnl, "equity": equity})
                pos = None
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak)
                if not day_locked and day_pnl <= -config.DAILY_MAX_LOSS * day_start:
                    day_locked = True
                    breaker_days += 1

        if pos is not None or day_locked:
            continue
        if not (config.SESSION_START_UTC <= hours[i] < config.SESSION_END_UTC):
            continue

        direction, _ = se.decide(c[i], h1e[i], fast[i - 1], slow[i - 1], fast[i], slow[i], rsi_v[i], atr_v[i])
        if direction is None:
            continue
        entry = o[i + 1] + (spread if direction == "BUY" else 0.0)
        sl, tp, dist = stop_levels(direction, entry, atr_v[i], digits=2)
        pos = {"dir": direction, "entry": entry, "sl": sl, "tp": tp, "dist": dist,
               "risk": equity * config.RISK_PER_TRADE, "entry_i": i + 1,
               "time": df["time"].iloc[i + 1]}

    t = pd.DataFrame(trades)
    stats = {"bars": len(df), "from": str(df["time"].iloc[0]), "to": str(df["time"].iloc[-1]),
             "trades": len(t), "breaker_days": breaker_days,
             "start_equity": start_equity, "end_equity": round(float(equity), 2),
             "net_return_pct": round(float(equity / start_equity - 1) * 100, 2),
             "max_drawdown_pct": round(float(max_dd) * 100, 2)}
    if len(t):
        wins, losses = t[t["pnl"] > 0], t[t["pnl"] <= 0]
        gross_loss = -losses["pnl"].sum()
        stats.update({
            "win_rate_pct": round(len(wins) / len(t) * 100, 1),
            "avg_r": round(float(t["r"].mean()), 3),
            "profit_factor": round(float(wins["pnl"].sum() / gross_loss), 2) if gross_loss > 0 else float("inf"),
            "buys": int((t["dir"] == "BUY").sum()), "sells": int((t["dir"] == "SELL").sum()),
        })
    return stats, t


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest xau_rulebot rules")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="M5 CSV file (time, open, high, low, close)")
    src.add_argument("--mt5", action="store_true", help="pull M5 history from MT5")
    p.add_argument("--days", type=int, default=365, help="days of history with --mt5")
    p.add_argument("--utc-offset", type=float, default=0.0, help="broker server time minus UTC, hours")
    p.add_argument("--spread", type=float, default=0.30, help="spread in price units ($)")
    p.add_argument("--equity", type=float, default=10_000.0)
    args = p.parse_args()

    m5 = load_csv(args.csv) if args.csv else load_mt5(args.days)
    stats, trades = run_backtest(m5, args.utc_offset, args.spread, args.equity)

    print("\n=== xau_rulebot backtest (news lockout NOT simulated) ===")
    for k, v in stats.items():
        print(f"{k:>18}: {v}")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DATA_DIR / "backtest_trades.csv"
    trades.to_csv(out, index=False)
    print(f"\nTrade list saved to {out}")


if __name__ == "__main__":
    main()
