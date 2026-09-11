"""
MetaTrader 5 bridge: connection, closed-bar market data, account checks,
bracket orders (market entry with SL + TP attached) and emergency close-out.
"""
from __future__ import annotations

import logging

import pandas as pd

import config
from risk_manager import position_size, stop_levels

log = logging.getLogger("broker")


class BrokerError(RuntimeError):
    pass


class OrderResult:
    """Truthy when the order was placed (or logged, in dry-run)."""

    def __init__(self, ok: bool, status: str, plan: dict, entry: float | None = None,
                 ticket: int | None = None, detail: str = ""):
        self.ok, self.status, self.plan = ok, status, plan
        self.entry = entry if entry is not None else plan.get("entry")
        self.ticket, self.detail = ticket, detail

    def __bool__(self) -> bool:
        return self.ok


class Broker:
    _TIMEFRAMES = {"M5": "TIMEFRAME_M5", "H1": "TIMEFRAME_H1"}

    def __init__(self, db, mt5_module=None):
        if mt5_module is None:
            try:
                import MetaTrader5 as mt5_module
            except ImportError as exc:
                raise BrokerError("MetaTrader5 package missing (Windows only): pip install MetaTrader5") from exc
        self.mt5 = mt5_module
        self.db = db

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        kwargs = {}
        if config.MT5_PATH:
            kwargs["path"] = config.MT5_PATH
        if config.MT5_LOGIN:
            kwargs.update(login=config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        if not self.mt5.initialize(**kwargs):
            raise BrokerError(f"MT5 initialize failed: {self.mt5.last_error()}")
        if not self.mt5.symbol_select(config.SYMBOL, True):
            raise BrokerError(f"Symbol {config.SYMBOL} not available: {self.mt5.last_error()} "
                              f"(check your broker's gold symbol name and set SYMBOL in .env)")
        log.info("Connected to MT5, symbol %s selected", config.SYMBOL)

    def ensure_connected(self) -> None:
        if self.mt5.terminal_info() is None:
            log.warning("MT5 connection lost, reconnecting")
            self.mt5.shutdown()
            self.connect()

    def shutdown(self) -> None:
        self.mt5.shutdown()

    # ------------------------------------------------------------ account
    def account(self):
        info = self.mt5.account_info()
        if info is None:
            raise BrokerError(f"account_info failed: {self.mt5.last_error()}")
        return info

    def is_demo(self) -> bool:
        return self.account().trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    def enforce_account_gate(self) -> None:
        if not self.is_demo() and not config.ALLOW_LIVE:
            raise BrokerError("Real-money account detected. Trading refused. "
                              "Set ALLOW_LIVE=true in .env only after demo testing.")
        term = self.mt5.terminal_info()
        if term is not None and not term.trade_allowed and not config.DRY_RUN:
            raise BrokerError("Algo Trading is switched off in the MT5 terminal. Turn it on and restart.")

    # ------------------------------------------------------------ market data
    def closed_bars(self, timeframe: str, count: int) -> pd.DataFrame:
        """Closed bars only (position 1 skips the bar still forming), oldest first.
        Note: MT5 bar times are broker SERVER time, not UTC."""
        tf = getattr(self.mt5, self._TIMEFRAMES[timeframe])
        rates = self.mt5.copy_rates_from_pos(config.SYMBOL, tf, 1, count)
        if rates is None or len(rates) == 0:
            raise BrokerError(f"No {timeframe} data for {config.SYMBOL}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)

    def open_positions(self) -> list:
        positions = self.mt5.positions_get(symbol=config.SYMBOL)
        if positions is None:
            raise BrokerError(f"positions_get failed: {self.mt5.last_error()}")
        return [p for p in positions if p.magic == config.MAGIC_NUMBER]

    # ------------------------------------------------------------ orders
    def _filling(self, info) -> int:
        mode = info.filling_mode           # bit 1 = FOK allowed, bit 2 = IOC allowed
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def tick(self):
        tick = self.mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            raise BrokerError(f"No tick for {config.SYMBOL}: {self.mt5.last_error()}")
        return tick

    def preview_plan(self, direction: str, atr_value: float) -> dict:
        """Entry / SL / TP / lot size for a trade at the current price. Sends nothing."""
        info = self.mt5.symbol_info(config.SYMBOL)
        if info is None:
            raise BrokerError(f"No symbol info: {self.mt5.last_error()}")
        tick = self.tick()
        acct = self.account()
        price = tick.ask if direction == "BUY" else tick.bid
        sl, tp, dist = stop_levels(direction, price, atr_value, info.digits)
        tick_value = getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value
        size = position_size(acct.equity, dist, info.trade_tick_size, tick_value,
                             info.volume_min, info.volume_max, info.volume_step)
        min_dist = (info.trade_stops_level or 0) * info.point
        note = size.reason
        if dist <= min_dist:
            note = f"SL distance {dist:.2f} is inside the broker's minimum stop distance {min_dist:.2f}"
        return {
            "direction": direction, "entry": price, "sl": sl, "tp": tp,
            "sl_distance": round(dist, info.digits), "tp_distance": round(dist * config.RISK_REWARD, info.digits),
            "volume": size.volume, "risk_amount": round(size.risk_amount, 2),
            "reward_amount": round(size.risk_amount * config.RISK_REWARD, 2),
            "atr": atr_value, "equity": acct.equity, "digits": info.digits,
            "tradable": size.volume > 0 and dist > min_dist, "note": note,
            "_filling": self._filling(info),
        }

    def place_market_order(self, direction: str, atr_value: float) -> OrderResult:
        mt5 = self.mt5
        plan = self.preview_plan(direction, atr_value)
        base = dict(direction=direction, entry=plan["entry"], sl=plan["sl"], tp=plan["tp"],
                    atr=atr_value, equity=plan["equity"])

        if not plan["tradable"]:
            log.warning("Trade skipped: %s", plan["note"])
            self.db.log_trade(ticket=None, volume=0, risk_amount=0, status="SKIPPED",
                              detail=plan["note"], **base)
            return OrderResult(False, "SKIPPED", plan, detail=plan["note"])

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": plan["volume"],
            "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": plan["entry"],
            "sl": plan["sl"],
            "tp": plan["tp"],
            "deviation": config.MAX_SLIPPAGE_POINTS,
            "magic": config.MAGIC_NUMBER,
            "comment": config.ORDER_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": plan["_filling"],
        }
        volume, risk = plan["volume"], plan["risk_amount"]

        if config.DRY_RUN:
            log.info("DRY RUN order: %s", request)
            self.db.log_trade(ticket=None, volume=volume, risk_amount=risk, status="DRY_RUN", **base)
            return OrderResult(True, "DRY_RUN", plan)

        check = mt5.order_check(request)
        if check is None or check.retcode != 0:
            detail = f"order_check failed: {getattr(check, 'retcode', None)} {getattr(check, 'comment', mt5.last_error())}"
            log.error(detail)
            self.db.log_trade(ticket=None, volume=volume, risk_amount=risk, status="REJECTED", detail=detail, **base)
            return OrderResult(False, "REJECTED", plan, detail=detail)

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            detail = f"order_send failed: {getattr(result, 'retcode', None)} {getattr(result, 'comment', mt5.last_error())}"
            log.error(detail)
            self.db.log_trade(ticket=None, volume=volume, risk_amount=risk, status="FAILED", detail=detail, **base)
            return OrderResult(False, "FAILED", plan, detail=detail)

        base["entry"] = result.price or plan["entry"]
        log.info("ORDER FILLED %s %.2f lots @ %.2f SL %.2f TP %.2f (risk %.2f)",
                 direction, volume, base["entry"], plan["sl"], plan["tp"], risk)
        self.db.log_trade(ticket=result.order, volume=volume, risk_amount=risk, status="OPEN", **base)
        return OrderResult(True, "OPEN", plan, entry=base["entry"], ticket=result.order)

    def position_result(self, ticket: int) -> dict | None:
        """Outcome of a closed position from deal history, or None if not synced yet."""
        deals = self.mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        exits = [d for d in deals if d.entry in (1, 2, 3)]      # OUT, INOUT, OUT_BY
        if not exits:
            return None
        last = exits[-1]
        profit = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        reason = {4: "Stop loss hit", 5: "Take profit hit"}.get(getattr(last, "reason", -1), "Closed")
        return {"ticket": ticket, "profit": round(profit, 2), "close_price": last.price,
                "volume": sum(d.volume for d in exits), "reason": reason}

    def close_all_positions(self, reason: str) -> int:
        """Market-close every position opened by this bot. Returns number of failures."""
        mt5 = self.mt5
        failures = 0
        for p in self.open_positions():
            tick = mt5.symbol_info_tick(p.symbol)
            info = mt5.symbol_info(p.symbol)
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": tick.bid if is_buy else tick.ask,
                "deviation": config.MAX_SLIPPAGE_POINTS,
                "magic": config.MAGIC_NUMBER,
                "comment": f"close {reason}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(info),
            }
            if config.DRY_RUN:
                log.info("DRY RUN close: %s", request)
                continue
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                failures += 1
                log.error("Close failed for ticket %s: %s", p.ticket, getattr(result, "comment", mt5.last_error()))
            else:
                self.db.log_trade(ticket=p.ticket, direction="BUY" if is_buy else "SELL",
                                  volume=p.volume, entry=p.price_open, sl=p.sl, tp=p.tp, atr=0,
                                  risk_amount=0, equity=self.account().equity,
                                  status="CLOSED", detail=reason)
        return failures
