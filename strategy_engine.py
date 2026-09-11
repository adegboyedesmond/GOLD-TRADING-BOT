"""
Technical checklist (spec 2A). Evaluated on CLOSED bars only.

BUY  when: M5 close > H1 EMA200  AND  M5 EMA9 crosses above EMA21  AND  40 <= RSI <= 60
SELL when: M5 close < H1 EMA200  AND  M5 EMA9 crosses below EMA21  AND  40 <= RSI <= 60

`decide()` holds the rule logic and is shared by the live bot and backtest.py,
so both run exactly the same rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import config


# ---------------------------------------------------------------- indicators
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def add_indicators(m5: pd.DataFrame) -> pd.DataFrame:
    df = m5.copy()
    df["ema_fast"] = ema(df["close"], config.M5_FAST_EMA)
    df["ema_slow"] = ema(df["close"], config.M5_SLOW_EMA)
    df["rsi"] = rsi(df["close"], config.RSI_PERIOD)
    df["atr"] = atr(df, config.ATR_PERIOD)
    return df


# ---------------------------------------------------------------- rules
def decide(close: float, h1_ema: float, fast_prev: float, slow_prev: float,
           fast_now: float, slow_now: float, rsi_now: float, atr_now: float
           ) -> tuple[Optional[str], dict]:
    values = (close, h1_ema, fast_prev, slow_prev, fast_now, slow_now, rsi_now, atr_now)
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in values):
        return None, {"error": "insufficient data"}

    trend = "UP" if close > h1_ema else "DOWN" if close < h1_ema else "FLAT"
    if fast_prev <= slow_prev and fast_now > slow_now:
        cross = "UP"
    elif fast_prev >= slow_prev and fast_now < slow_now:
        cross = "DOWN"
    else:
        cross = None
    rsi_ok = config.RSI_MIN <= rsi_now <= config.RSI_MAX

    checks = {
        "close": round(float(close), 3), "h1_ema200": round(float(h1_ema), 3), "trend": trend,
        "ema_fast": round(float(fast_now), 3), "ema_slow": round(float(slow_now), 3),
        "ema_cross": cross, "rsi": round(float(rsi_now), 2), "rsi_ok": rsi_ok,
        "atr": round(float(atr_now), 3),
    }
    if atr_now <= 0:
        return None, checks
    if trend == "UP" and cross == "UP" and rsi_ok:
        return "BUY", checks
    if trend == "DOWN" and cross == "DOWN" and rsi_ok:
        return "SELL", checks
    return None, checks


@dataclass
class Signal:
    direction: Optional[str]
    atr: float = float("nan")
    price: float = float("nan")
    checks: dict = field(default_factory=dict)


def evaluate(m5: pd.DataFrame, h1: pd.DataFrame) -> Signal:
    """m5 / h1: closed bars, oldest first, columns open/high/low/close."""
    min_m5 = max(config.M5_SLOW_EMA, config.RSI_PERIOD, config.ATR_PERIOD) * 3
    if len(h1) < config.H1_TREND_EMA or len(m5) < min_m5:
        return Signal(None, checks={"error": f"not enough history (h1={len(h1)}, m5={len(m5)})"})

    h1_ema = float(ema(h1["close"], config.H1_TREND_EMA).iloc[-1])
    df = add_indicators(m5)
    last, prev = df.iloc[-1], df.iloc[-2]
    direction, checks = decide(float(last["close"]), h1_ema,
                               float(prev["ema_fast"]), float(prev["ema_slow"]),
                               float(last["ema_fast"]), float(last["ema_slow"]),
                               float(last["rsi"]), float(last["atr"]))
    return Signal(direction, float(last["atr"]), float(last["close"]), checks)
