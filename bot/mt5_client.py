"""Trades through a MetaTrader 5 terminal instead of the Deriv API.

Uses the official `MetaTrader5` Python package, which only works on Windows
Python with the MT5 terminal installed and running on the same machine (it
talks to the terminal, not to the broker). It does not work inside WSL,
Docker or Linux.

The client speaks the same small set of messages the bot already uses with
Deriv (ticks, ticks_history, portfolio, proposal, buy, proposal_open_contract,
sell), so the main loop, the strategies, the risk limits and the trade log
are shared. What changes is what a trade is:

* Deriv multiplier: the stake is the most the trade can lose.
* MT5 position: a fixed lot size (MT5_VOLUME, default the symbol's minimum)
  with a stop loss placed where the loss equals the stake (or STOP_LOSS if it
  is smaller). So STAKE still means "the most one trade should lose", and
  MAX_DAILY_LOSS still counts every open trade at its stake.

A stop loss is an order, not a guarantee: a price gap (weekend, news) or
commission can make a trade lose a little more than its stake. The client
only sees positions carrying its magic number (MT5_MAGIC), so trades you
place by hand on the same account are left alone.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import logging
import math
import time
from typing import Any

from .config import Config
from .deriv_client import DerivError, MessageHandler, NotConnected

log = logging.getLogger(__name__)

TICK_POLL = 0.25
POSITION_POLL = 1.0
RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_HINTS = {
    10018: "the market is closed",
    10019: "not enough money on the account",
    10027: "Algo Trading is switched off in MetaTrader 5 (press the Algo Trading button)",
    10016: "the stop loss is too close to the price; raise STAKE or lower MT5_VOLUME",
}
TIMEFRAMES = {60: "M1", 120: "M2", 180: "M3", 300: "M5", 600: "M10", 900: "M15",
              1800: "M30", 3600: "H1", 7200: "H2", 14400: "H4", 86400: "D1"}


class Mt5Error(DerivError):
    """MetaTrader 5 refused something. `fatal` errors stop the bot."""

    def __init__(self, message: str, code: str = "", fatal: bool = False):
        super().__init__(message, code)
        self.fatal = fatal


def load_mt5():
    try:
        import MetaTrader5  # type: ignore
    except ImportError:
        raise Mt5Error(
            "the MetaTrader5 package is not installed. It only runs on Windows Python "
            "(not WSL or Docker): in PowerShell run  pip install MetaTrader5",
            "NotInstalled", fatal=True) from None
    return MetaTrader5


class Mt5Client:
    def __init__(self, cfg: Config, on_message: MessageHandler, mt5_module: Any = None):
        self.cfg = cfg
        self.on_message = on_message
        self.mt5 = mt5_module
        self.account: dict | None = None
        self.symbol = cfg.symbol
        self._info = None
        self._connected = False
        self._streaming = False
        self._tracked: set[int] = set()
        self._proposals: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._last_tick = 0
        self._offset = cfg.mt5_utc_offset_hours * 3600
        # The MT5 package keeps one global connection: never call it from
        # two threads at once.
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="mt5")

    async def _call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))

    def _error(self, what: str) -> str:
        try:
            code, text = self.mt5.last_error()
            return f"{what}: {text} ({code})"
        except Exception:
            return what

    def _epoch(self, server_seconds: float) -> int:
        """MT5 stamps prices in the broker's server time; the bot uses UTC."""
        return int(server_seconds - self._offset)

    # ---- connection ----------------------------------------------------

    async def connect(self) -> None:
        if self.mt5 is None:
            self.mt5 = load_mt5()
        mt5, cfg = self.mt5, self.cfg
        kwargs: dict = {}
        if cfg.mt5_path:
            kwargs["path"] = cfg.mt5_path
        if cfg.mt5_login:
            kwargs.update(login=int(cfg.mt5_login), password=cfg.mt5_password,
                          server=cfg.mt5_server)
        if not await self._call(mt5.initialize, **kwargs):
            message = self._error("could not reach the MetaTrader 5 terminal")
            await self._call(mt5.shutdown)
            # A wrong login stops the bot; a closed terminal is retried.
            if cfg.mt5_login and "auth" in message.lower():
                raise Mt5Error(message, "AuthorizationRequired", fatal=True)
            raise NotConnected(message + ". Is MT5 open and logged in?")

        acc = await self._call(mt5.account_info)
        if acc is None:
            raise NotConnected(self._error("MT5 is not logged in to an account"))
        is_demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
        if not is_demo and not cfg.is_real:
            await self.close()
            raise Mt5Error(f"Safety stop: MT5 account {acc.login} is not a demo account "
                           "and the bot is set to demo", "PermissionDenied", fatal=True)
        if is_demo and cfg.is_real:
            await self.close()
            raise Mt5Error(f"MT5 account {acc.login} is a demo account but the bot is set "
                           "to real", "PermissionDenied", fatal=True)
        hedging = acc.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        if not hedging and cfg.max_open_trades > 1:
            await self.close()
            raise Mt5Error("this MT5 account nets positions together, so MAX_OPEN_TRADES "
                           "must be 1", "InvalidSettings", fatal=True)

        if not await self._call(mt5.symbol_select, self.symbol, True):
            await self.close()
            raise Mt5Error(self._error(f"MT5 has no symbol {self.symbol!r}; use the name "
                                       "shown in Market Watch"), "InvalidSymbol", fatal=True)
        self._info = await self._call(mt5.symbol_info, self.symbol)
        terminal = await self._call(mt5.terminal_info)
        if terminal is not None and not terminal.trade_allowed:
            log.warning("Algo Trading is off in MetaTrader 5: the bot can watch but "
                        "trades will be refused until you press the Algo Trading button")

        self.account = {
            "account_id": str(acc.login),
            "account_type": "demo" if is_demo else "real",
            "currency": acc.currency,
            "balance": acc.balance,
            "server": acc.server,
        }
        self._connected = True
        self._check_clock(await self._call(mt5.symbol_info_tick, self.symbol))
        log.info("Connected to MT5 %s account %s on %s (%s %.2f), %s",
                 self.account["account_type"], acc.login, acc.server, acc.currency,
                 float(acc.balance or 0), "hedging" if hedging else "netting")

    def _check_clock(self, tick) -> None:
        if tick is None:
            return
        drift = self._epoch(tick.time) - time.time()
        if 3000 < abs(drift) < 14 * 3600:
            log.warning("MT5 prices look %+.1f hours off UTC. If this market is open, set "
                        "MT5_SERVER_UTC_OFFSET to the broker's server time zone.",
                        drift / 3600)

    @property
    def connected(self) -> bool:
        return self._connected

    async def close(self) -> None:
        self._connected = False
        self._streaming = False
        if self.mt5 is not None:
            await self._call(self.mt5.shutdown)

    async def reader(self) -> None:
        """Poll prices and the tracked positions, and pass them on as the
        same messages Deriv would stream. Returns when the client closes."""
        if not self._connected:
            raise NotConnected()
        mt5 = self.mt5
        next_positions = 0.0
        while self._connected:
            if self._streaming:
                tick = await self._call(mt5.symbol_info_tick, self.symbol)
                if tick is not None and tick.time_msc != self._last_tick:
                    self._last_tick = tick.time_msc
                    await self.on_message(self._tick_msg(tick))
            if time.monotonic() >= next_positions:
                next_positions = time.monotonic() + POSITION_POLL
                terminal = await self._call(mt5.terminal_info)
                if terminal is None or not terminal.connected:
                    raise NotConnected("MetaTrader 5 lost its connection to the broker")
                for ticket in list(self._tracked):
                    try:
                        msg = await self._contract_msg(ticket)
                    except Mt5Error as exc:  # history can lag a moment behind a close
                        log.debug("Position %s: %s", ticket, exc)
                        continue
                    if msg["proposal_open_contract"].get("is_sold"):
                        self._tracked.discard(ticket)
                    await self.on_message(msg)
            await asyncio.sleep(TICK_POLL)

    # ---- requests --------------------------------------------------------

    async def request(self, payload: dict, timeout: float = 15.0) -> dict:
        if not self._connected:
            raise NotConnected("not connected to MetaTrader 5")
        if "ticks" in payload:
            return await self._ticks(payload)
        if "ticks_history" in payload:
            return await self._history(payload)
        if "portfolio" in payload:
            return await self._portfolio()
        if "proposal_open_contract" in payload:
            ticket = int(payload["contract_id"])
            if payload.get("subscribe"):
                self._tracked.add(ticket)
            return await self._contract_msg(ticket)
        if "proposal" in payload:
            return await self._proposal(payload)
        if "buy" in payload:
            return await self._buy(payload)
        if "sell" in payload:
            return await self._sell(int(payload["sell"]))
        raise Mt5Error(f"MT5 cannot do {sorted(payload)}")

    def _tick_msg(self, tick) -> dict:
        return {"msg_type": "tick", "tick": {"quote": tick.bid, "epoch": self._epoch(tick.time),
                                             "ask": tick.ask, "symbol": self.symbol}}

    async def _ticks(self, payload: dict) -> dict:
        tick = await self._call(self.mt5.symbol_info_tick, self.symbol)
        if tick is None:
            raise Mt5Error(self._error(f"no price for {self.symbol}"))
        if payload.get("subscribe"):
            self._streaming = True
            self._last_tick = tick.time_msc
        return self._tick_msg(tick)

    async def _history(self, payload: dict) -> dict:
        seconds = int(payload.get("granularity") or 60)
        name = TIMEFRAMES.get(seconds)
        if name is None:
            raise Mt5Error(f"MT5 has no {seconds}s candles")
        count = int(payload.get("count") or 100)
        rates = await self._call(self.mt5.copy_rates_from_pos, self.symbol,
                                 getattr(self.mt5, f"TIMEFRAME_{name}"), 0, count)
        if rates is None:
            raise Mt5Error(self._error("could not read candles"))
        return {"msg_type": "candles", "candles": [
            {"epoch": self._epoch(int(r["time"])), "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
            for r in rates]}

    async def _portfolio(self) -> dict:
        positions = await self._call(self.mt5.positions_get) or ()
        contracts = []
        for p in positions:
            if p.magic != self.cfg.mt5_magic:
                continue
            buy = p.type == self.mt5.POSITION_TYPE_BUY
            risk = 0.0
            if not p.sl:
                log.warning("Open MT5 position %s has no stop loss; its risk is not "
                            "counted in MAX_DAILY_LOSS", p.ticket)
            else:
                loss = await self._call(self.mt5.order_calc_profit, self._order_type(buy),
                                        p.symbol, p.volume, p.price_open, p.sl)
                risk = round(abs(min(loss or 0.0, 0.0)), 2)
            contracts.append({"contract_id": p.ticket, "buy_price": risk,
                              "contract_type": "MULTUP" if buy else "MULTDOWN"})
        return {"msg_type": "portfolio", "portfolio": {"contracts": contracts}}

    def _order_type(self, buy: bool) -> int:
        return self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL

    async def _proposal(self, payload: dict) -> dict:
        """Work out the lot size and the stop loss and take profit prices
        for a trade that risks `amount`. Nothing is sent to the broker."""
        mt5, info = self.mt5, self._info
        buy = payload["contract_type"] == "MULTUP"
        stake = float(payload["amount"])
        limits = payload.get("limit_order") or {}
        risk = min(stake, float(limits.get("stop_loss") or stake))

        volume = self.cfg.mt5_volume or info.volume_min
        if volume < info.volume_min or volume > info.volume_max:
            raise Mt5Error(f"MT5_VOLUME {volume} is outside {info.volume_min}.."
                           f"{info.volume_max} for {self.symbol}", "InvalidSettings")
        step = info.volume_step or info.volume_min
        volume = round(math.floor(volume / step + 1e-9) * step, 8)

        tick = await self._call(mt5.symbol_info_tick, self.symbol)
        if tick is None:
            raise Mt5Error(self._error(f"no price for {self.symbol}"))
        price = tick.ask if buy else tick.bid
        size = info.trade_tick_size or info.point
        side = -1 if buy else 1  # which way the stop loss sits from the price

        probe = 1000 * size
        loss = await self._call(mt5.order_calc_profit, self._order_type(buy), self.symbol,
                                volume, price, price + side * probe)
        if not loss or loss >= 0:
            raise Mt5Error(self._error("MT5 could not value this trade"))
        per_price = -loss / probe

        def stop_at(amount: float) -> float:
            steps = math.floor(amount / per_price / size + 1e-9)
            return round(price + side * steps * size, info.digits)

        sl = stop_at(risk)
        actual = await self._call(mt5.order_calc_profit, self._order_type(buy), self.symbol,
                                  volume, price, sl)
        actual = round(abs(actual or 0.0), 2)
        min_gap = (info.trade_stops_level or 0) * info.point + abs(tick.ask - tick.bid)
        if abs(price - sl) <= min_gap or actual <= 0:
            raise Mt5Error(f"a {risk:.2f} stop loss is too close to the price for "
                           f"{volume} lots of {self.symbol}; raise STAKE or lower MT5_VOLUME",
                           "StopTooClose")
        tp = 0.0
        if limits.get("take_profit"):
            steps = math.floor(float(limits["take_profit"]) / per_price / size + 1e-9)
            tp = round(price - side * steps * size, info.digits)

        pid = f"mt5-{next(self._ids)}"
        self._proposals = {pid: {"buy": buy, "volume": volume, "sl": sl, "tp": tp,
                                 "risk": actual}}
        return {"msg_type": "proposal", "proposal": {
            "id": pid, "ask_price": actual, "spot": price, "volume": volume,
            "stop_loss_price": sl, "take_profit_price": tp}}

    def _filling(self) -> int:
        mode = self._info.filling_mode or 0
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    async def _send(self, request: dict):
        result = await self._call(self.mt5.order_send, request)
        if result is None:
            raise Mt5Error(self._error("MT5 did not accept the order"))
        if result.retcode not in (RETCODE_DONE, RETCODE_PLACED):
            hint = RETCODE_HINTS.get(result.retcode, result.comment)
            raise Mt5Error(f"MT5 refused the order: {hint}", str(result.retcode))
        return result

    async def _buy(self, payload: dict) -> dict:
        quote = self._proposals.pop(str(payload["buy"]), None)
        if quote is None:
            raise Mt5Error("that quote is no longer valid")
        mt5 = self.mt5
        tick = await self._call(mt5.symbol_info_tick, self.symbol)
        buy = quote["buy"]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
            "volume": quote["volume"], "type": self._order_type(buy),
            "price": tick.ask if buy else tick.bid, "sl": quote["sl"], "tp": quote["tp"],
            "deviation": self.cfg.mt5_deviation, "magic": self.cfg.mt5_magic,
            "comment": "trading-bot", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(),
        }
        result = await self._send(request)
        ticket = result.order
        if result.deal:
            deals = await self._call(mt5.history_deals_get, ticket=result.deal) or ()
            if deals:
                ticket = deals[0].position_id or ticket
        return {"msg_type": "buy", "buy": {
            "contract_id": ticket, "buy_price": quote["risk"], "start_time": int(time.time()),
            "entry_price": result.price, "volume": result.volume}}

    async def _position(self, ticket: int):
        positions = await self._call(self.mt5.positions_get, ticket=ticket) or ()
        return positions[0] if positions else None

    async def _sell(self, ticket: int) -> dict:
        mt5 = self.mt5
        pos = await self._position(ticket)
        if pos is None:
            return {"msg_type": "sell", "sell": {"contract_id": ticket}}
        buy = pos.type == mt5.POSITION_TYPE_BUY
        tick = await self._call(mt5.symbol_info_tick, pos.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
            "type": self._order_type(not buy), "position": ticket,
            "price": tick.bid if buy else tick.ask, "deviation": self.cfg.mt5_deviation,
            "magic": self.cfg.mt5_magic, "comment": "trading-bot close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(),
        }
        result = await self._send(request)
        return {"msg_type": "sell", "sell": {"contract_id": ticket, "sold_for": result.price}}

    async def _contract_msg(self, ticket: int) -> dict:
        pos = await self._position(ticket)
        if pos is not None:
            return {"msg_type": "proposal_open_contract", "proposal_open_contract": {
                "contract_id": ticket, "is_sold": 0, "date_start": self._epoch(pos.time),
                "profit": pos.profit, "entry_spot": pos.price_open}}
        deals = await self._call(self.mt5.history_deals_get, position=ticket) or ()
        if not deals:
            raise Mt5Error(f"MT5 has no position or history for {ticket}", "ContractNotFound")
        profit = round(sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                           for d in deals), 2)
        exits = [d for d in deals if d.entry != self.mt5.DEAL_ENTRY_IN]
        reason = "closed"
        if exits:
            last = exits[-1]
            reason = {self.mt5.DEAL_REASON_SL: "stop loss",
                      self.mt5.DEAL_REASON_TP: "take profit"}.get(last.reason, "closed")
        return {"msg_type": "proposal_open_contract", "proposal_open_contract": {
            "contract_id": ticket, "is_sold": 1, "profit": profit,
            "status": f"{'won' if profit >= 0 else 'lost'} ({reason})",
            "exit_spot": exits[-1].price if exits else ""}}
