"""
Risk controls (spec 2D): position sizing, ATR stop/target, daily drawdown breaker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import config

BREAKER_KEY = "daily_breaker"


# ---------------------------------------------------------------- stops
def stop_levels(direction: str, entry: float, atr_value: float, digits: int = 2
                ) -> tuple[float, float, float]:
    """Returns (sl, tp, sl_distance). SL = 1.5 x ATR, TP = 2 x SL distance."""
    dist = atr_value * config.SL_ATR_MULTIPLIER
    if direction == "BUY":
        sl, tp = entry - dist, entry + dist * config.RISK_REWARD
    elif direction == "SELL":
        sl, tp = entry + dist, entry - dist * config.RISK_REWARD
    else:
        raise ValueError(f"bad direction {direction!r}")
    return round(sl, digits), round(tp, digits), dist


# ---------------------------------------------------------------- sizing
@dataclass
class SizeResult:
    volume: float
    risk_amount: float
    loss_per_lot: float
    reason: str = ""


def position_size(equity: float, sl_distance: float, tick_size: float, tick_value: float,
                  volume_min: float, volume_max: float, volume_step: float,
                  risk_fraction: float = config.RISK_PER_TRADE) -> SizeResult:
    """Largest lot size whose stop-out loss does not exceed risk_fraction of equity.
    Rounds DOWN; if even the broker minimum lot would exceed the budget, returns 0."""
    if min(equity, sl_distance, tick_size, tick_value, volume_step) <= 0:
        return SizeResult(0.0, 0.0, 0.0, "invalid sizing inputs")
    budget = equity * risk_fraction
    loss_per_lot = (sl_distance / tick_size) * tick_value
    steps = math.floor(budget / loss_per_lot / volume_step + 1e-9)
    volume = min(steps * volume_step, volume_max)
    decimals = max(0, -int(math.floor(math.log10(volume_step)))) if volume_step < 1 else 0
    volume = round(volume, decimals)
    if volume < volume_min - 1e-12:
        return SizeResult(0.0, 0.0, loss_per_lot,
                          f"1% risk ({budget:.2f}) is smaller than min lot {volume_min} "
                          f"would lose ({volume_min * loss_per_lot:.2f})")
    return SizeResult(volume, volume * loss_per_lot, loss_per_lot)


# ---------------------------------------------------------------- daily breaker
@dataclass
class BreakerStatus:
    tripped: bool
    just_tripped: bool
    day: str
    start_equity: float
    realized_pnl: float
    loss_limit: float


class DailyBreaker:
    """Trips when today's closed P&L <= -3% of the day's starting equity.

    Day = UTC calendar day. Closed P&L = balance now - balance at day start
    (balance only moves on closed trades, so deposits/withdrawals mid-day
    will distort it). State is stored in SQLite so restarts keep the lock.
    """

    def __init__(self, db):
        self.db = db

    def update(self, now: datetime, balance: float, equity: float) -> BreakerStatus:
        day = now.strftime("%Y-%m-%d")
        st = self.db.get_state(BREAKER_KEY)
        if not st or st.get("day") != day:
            carry = bool(st and st.get("tripped") and not config.BREAKER_AUTO_RESET_DAILY)
            if st and st.get("tripped") and not carry:
                self.db.audit("INFO", f"Daily breaker auto-reset for new day {day}")
            st = {"day": day, "start_balance": balance, "start_equity": equity,
                  "tripped": carry, "tripped_at": st.get("tripped_at") if carry else None}
            self.db.set_state(BREAKER_KEY, st)

        realized = balance - st["start_balance"]
        limit = config.DAILY_MAX_LOSS * st["start_equity"]
        just = False
        if not st["tripped"] and realized <= -limit:
            st["tripped"] = True
            st["tripped_at"] = now.isoformat()
            self.db.set_state(BREAKER_KEY, st)
            just = True
        return BreakerStatus(st["tripped"], just, day, st["start_equity"], realized, limit)

    def manual_reset(self) -> None:
        """Clears the lock; the next update() re-baselines from the current balance."""
        self.db.delete_state(BREAKER_KEY)
        self.db.audit("WARN", "Daily breaker manually reset")
