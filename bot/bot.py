"""The main loop: connect, watch prices, ask the strategy, check the risk
limits, trade, track every open contract until it closes, reconnect when
the connection drops.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .config import Config
from .deriv_client import DerivClient, DerivError, NotConnected
from .risk import RiskManager
from .strategies import Signal, Strategy, make_strategy
from .trade_log import TradeLog

log = logging.getLogger(__name__)

FATAL_CODES = {"401", "403", "InvalidToken", "AuthorizationRequired", "PermissionDenied"}
GUARD_INTERVAL = 2.0
SUMMARY_INTERVAL = 600.0


@dataclass
class OpenTrade:
    contract_id: str
    stake: float
    direction: str = ""
    started_at: float | None = None  # epoch seconds, from Deriv when known
    selling: bool = False


class TradingBot:
    def __init__(
        self,
        cfg: Config,
        client: DerivClient | None = None,
        strategy: Strategy | None = None,
        risk: RiskManager | None = None,
        trade_log: TradeLog | None = None,
    ):
        self.cfg = cfg
        self.client = client or DerivClient(cfg, self.handle_message)
        self.strategy = strategy or make_strategy(cfg.strategy)
        self.risk = risk or RiskManager(cfg)
        self.trades = trade_log or TradeLog(cfg.log_dir)
        self.open_trades: dict[str, OpenTrade] = {}
        self.currency = "USD"
        self._placing = False
        self._warmed_up = False
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._last_refusal = ""
        self._last_summary = time.monotonic()

        if self.strategy.demo_only and cfg.is_real:
            raise ValueError(f"Strategy {self.strategy.name!r} is for demo accounts only")

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.client.connect()
                backoff = 1.0
                await self._run_connected()
            except DerivError as exc:
                if exc.code in FATAL_CODES:
                    log.error("Deriv refused our credentials (%s). Check the token and App ID.", exc)
                    raise
                log.warning("Deriv error: %s", exc)
            except (OSError, NotConnected, asyncio.TimeoutError) as exc:
                log.warning("Connection problem: %s", exc or type(exc).__name__)
            except Exception as exc:  # websockets.ConnectionClosed and friends
                if type(exc).__module__.startswith("websockets"):
                    log.warning("Connection closed: %s", exc)
                else:
                    raise
            finally:
                await self.client.close()

            if not self._stop.is_set():
                log.info("Reconnecting in %.0fs", backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        log.info("Bot stopped. %s", self.risk.summary())

    async def _run_connected(self) -> None:
        reader = asyncio.create_task(self.client.reader(), name="reader")
        try:
            await self._after_connect()
        except BaseException:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        guard = asyncio.create_task(self._guard_loop(), name="guard")
        stopper = asyncio.create_task(self._stop.wait(), name="stopper")
        done, pending = await asyncio.wait(
            {reader, guard, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is not stopper and task.exception():
                raise task.exception()
        if reader in done and not self._stop.is_set():
            raise NotConnected("connection closed by server")

    async def _after_connect(self) -> None:
        self.currency = (self.client.account or {}).get("currency") or "USD"
        await self._adopt_open_contracts()
        await self._warm_up()
        self._reconcile_direction()
        first = await self.client.request({"ticks": self.cfg.symbol, "subscribe": 1})
        await self.handle_message(first)
        log.info("Watching %s with strategy %r%s", self.cfg.symbol, self.strategy.name,
                 " (dry run)" if self.cfg.dry_run else "")

    def _reconcile_direction(self) -> None:
        """EXIT_ON_REVERSE: if the trend turned while the bot was offline, the
        crossover was missed, so close trades pointing the old way now."""
        if not self.cfg.exit_on_reverse or self._placing:
            return
        side = self.strategy.current_direction()
        wrong = [t for t in self.open_trades.values() if side and t.direction
                 and t.direction != side]
        if wrong:
            log.info("Trend is %s but %d open trade(s) point the other way", side, len(wrong))
            self._placing = True
            self._spawn(self._reverse(wrong, Signal(side, "trend turned while the bot was offline")))

    async def _warm_up(self) -> None:
        """Feed recent candles to a candle-based strategy so it can trade
        straight away. Runs once; after a reconnect the strategy keeps its
        history. Signals during warm-up are ignored."""
        if self._warmed_up or not self.strategy.warmup_candles:
            return
        self._warmed_up = True
        try:
            msg = await self.client.request({
                "ticks_history": self.cfg.symbol, "style": "candles",
                "granularity": self.strategy.candle_seconds,
                "count": self.strategy.warmup_candles + 1, "end": "latest",
            })
        except DerivError as exc:
            log.warning("Could not preload candles (%s); building history from live prices", exc)
            return
        candles = msg.get("candles") or []
        # The last candle is the one still forming; live ticks carry it on.
        for c in candles:
            try:
                self.strategy.on_tick(float(c["close"]), int(c["epoch"]))
            except (KeyError, TypeError, ValueError):
                continue
        if candles:
            log.info("Preloaded %d candles of %ss", len(candles), self.strategy.candle_seconds)
        else:
            log.info("No recent candles (market closed?); building history from live prices")

    async def _adopt_open_contracts(self) -> None:
        """Re-attach to contracts that are open on the account, e.g. after a
        restart or a dropped connection. The bot assumes it owns every open
        contract on the account, so use an account dedicated to the bot."""
        ids = set(self.risk.state.open_stakes) | set(self.open_trades)
        directions: dict[str, str] = {}
        try:
            portfolio = await self.client.request({"portfolio": 1})
            for c in (portfolio.get("portfolio") or {}).get("contracts") or []:
                cid = str(c.get("contract_id"))
                kind = str(c.get("contract_type") or "")
                if kind in ("MULTUP", "MULTDOWN"):
                    directions[cid] = "UP" if kind == "MULTUP" else "DOWN"
                if cid not in self.risk.state.open_stakes:
                    stake = float(c.get("buy_price") or 0)
                    log.warning("Found open contract %s not in state; tracking it", cid)
                    self.risk.state.open_stakes[cid] = stake
                ids.add(cid)
        except DerivError as exc:
            log.warning("Could not read open contracts (%s); using saved state", exc)

        for cid in ids:
            stake = self.risk.state.open_stakes.get(cid, 0.0)
            trade = self.open_trades.setdefault(cid, OpenTrade(cid, stake))
            trade.direction = trade.direction or directions.get(cid, "")
            await self._track(cid)

    # ---- incoming messages ------------------------------------------------

    async def handle_message(self, msg: dict) -> None:
        kind = msg.get("msg_type")
        if kind == "tick":
            self._on_tick(msg.get("tick") or {})
        elif kind == "proposal_open_contract":
            self._on_contract(msg.get("proposal_open_contract") or {})
        elif "error" in msg:
            log.warning("Deriv error on stream: %s", (msg["error"] or {}).get("message"))

    def _on_tick(self, tick: dict) -> None:
        try:
            price, epoch = float(tick["quote"]), int(tick["epoch"])
        except (KeyError, TypeError, ValueError):
            return
        signal = self.strategy.on_tick(price, epoch)
        if signal is not None and not self._placing:
            self._placing = True
            opposite = [t for t in self.open_trades.values()
                        if t.direction and t.direction != signal.direction]
            if self.cfg.exit_on_reverse and opposite:
                self._spawn(self._reverse(opposite, signal))
            else:
                self._spawn(self.try_trade(signal))

    def _on_contract(self, poc: dict) -> None:
        cid = str(poc.get("contract_id", ""))
        trade = self.open_trades.get(cid)
        if trade is None:
            return
        if trade.started_at is None and poc.get("date_start"):
            trade.started_at = float(poc["date_start"])
        if not poc.get("is_sold"):
            return
        profit = float(poc.get("profit") or 0)
        self.open_trades.pop(cid, None)
        self.risk.record_close(cid, profit)
        self.strategy.on_trade_closed(profit)
        self.trades.write(
            "closed", account_type=self.cfg.account_type, symbol=self.cfg.symbol,
            direction=trade.direction, stake=trade.stake, multiplier=self.cfg.multiplier,
            contract_id=cid, buy_price=poc.get("buy_price", ""),
            sell_price=poc.get("sell_price", ""), profit=f"{profit:.2f}",
            reason=poc.get("status", ""),
        )
        log.info("Closed %s: %+.2f. %s", cid, profit, self.risk.summary())

    # ---- trading ---------------------------------------------------------

    async def try_trade(self, signal: Signal) -> None:
        try:
            await self._trade(signal)
        except (DerivError, NotConnected, asyncio.TimeoutError) as exc:
            log.error("Trade failed: %s", exc or type(exc).__name__)
            self.trades.write("error", account_type=self.cfg.account_type,
                              symbol=self.cfg.symbol, direction=signal.direction,
                              stake=self.cfg.stake, reason=str(exc))
        finally:
            self._placing = False

    async def _reverse(self, trades: list[OpenTrade], signal: Signal,
                       wait_seconds: float = 30.0) -> None:
        """EXIT_ON_REVERSE: close trades pointing the other way, wait for
        Deriv to confirm, then take the new signal."""
        try:
            for trade in trades:
                await self.sell(trade, f"signal reversed ({signal.reason})")
            deadline = time.monotonic() + wait_seconds
            while any(t.contract_id in self.open_trades for t in trades):
                if time.monotonic() > deadline:
                    log.warning("Old trade not closed yet; skipping signal %s", signal.direction)
                    self._placing = False
                    return
                await asyncio.sleep(0.2)
        except BaseException:
            self._placing = False
            raise
        await self.try_trade(signal)

    async def _trade(self, signal: Signal) -> None:
        cfg = self.cfg
        stake = cfg.stake
        common = dict(account_type=cfg.account_type, symbol=cfg.symbol,
                      direction=signal.direction, stake=stake, multiplier=cfg.multiplier)

        allowed, reason = self.risk.check(stake)
        if not allowed:
            if reason != self._last_refusal:
                log.info("Signal %s skipped: %s", signal.direction, reason)
                self.trades.write("skipped", reason=reason, **common)
            self._last_refusal = reason
            return
        self._last_refusal = ""

        if cfg.dry_run:
            log.info("DRY RUN: would buy %s %.2f (%s)", signal.direction, stake, signal.reason)
            self.trades.write("dry_run", reason=signal.reason, dry_run="yes", **common)
            return

        proposal = {
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": "MULTUP" if signal.direction == "UP" else "MULTDOWN",
            "currency": self.currency,
            "multiplier": cfg.multiplier,
            "underlying_symbol": cfg.symbol,
        }
        limit_order = {}
        if cfg.take_profit:
            limit_order["take_profit"] = cfg.take_profit
        if cfg.stop_loss:
            limit_order["stop_loss"] = cfg.stop_loss
        if limit_order:
            proposal["limit_order"] = limit_order

        quote = (await self.client.request(proposal)).get("proposal") or {}
        ask = float(quote.get("ask_price") or 0)
        if not quote.get("id") or ask <= 0 or ask > cfg.max_stake:
            raise DerivError(f"unusable proposal (ask {ask:.2f}, max {cfg.max_stake:.2f})")

        bought = (await self.client.request({"buy": quote["id"], "price": ask})).get("buy") or {}
        cid = str(bought.get("contract_id", ""))
        if not cid:
            raise DerivError("buy response had no contract_id")
        buy_price = float(bought.get("buy_price") or ask)

        self.risk.record_open(cid, buy_price)
        self.open_trades[cid] = OpenTrade(cid, buy_price, signal.direction,
                                          started_at=bought.get("start_time"))
        self.trades.write("opened", contract_id=cid, buy_price=f"{buy_price:.2f}",
                          reason=signal.reason, **common)
        log.info("Opened %s %s for %.2f (%s)", signal.direction, cid, buy_price, signal.reason)
        await self._track(cid)

    async def _track(self, cid: str) -> None:
        """Subscribe to updates for a contract until it closes."""
        try:
            first = await self.client.request(
                {"proposal_open_contract": 1, "contract_id": _api_id(cid), "subscribe": 1}
            )
            await self.handle_message(first)
        except DerivError as exc:
            log.warning("Cannot track contract %s (%s); dropping it from open trades", cid, exc)
            self.open_trades.pop(cid, None)
            self.risk.forget_open(cid)

    async def sell(self, trade: OpenTrade, why: str) -> None:
        if trade.selling:
            return
        trade.selling = True
        log.info("Closing %s: %s", trade.contract_id, why)
        try:
            await self.client.request({"sell": _api_id(trade.contract_id), "price": 0})
        except DerivError as exc:
            trade.selling = False
            log.warning("Could not close %s: %s", trade.contract_id, exc)

    # ---- safety loop -------------------------------------------------------

    async def _guard_loop(self) -> None:
        while not self._stop.is_set():
            if self.risk.kill_switch_on():
                await self._kill()
                return
            if self.cfg.max_trade_minutes:
                limit = self.cfg.max_trade_minutes * 60
                now = time.time()
                for trade in list(self.open_trades.values()):
                    if trade.started_at and now - float(trade.started_at) > limit:
                        self._spawn(self.sell(trade, f"open longer than {self.cfg.max_trade_minutes} min"))
            if time.monotonic() - self._last_summary > SUMMARY_INTERVAL:
                self._last_summary = time.monotonic()
                log.info("Status: %s", self.risk.summary())
            await asyncio.sleep(GUARD_INTERVAL)

    async def _kill(self) -> None:
        log.warning("Kill switch %s found: closing open trades and stopping",
                    self.cfg.kill_switch_file)
        for trade in list(self.open_trades.values()):
            await self.sell(trade, "kill switch")
        for _ in range(15):
            if not self.open_trades:
                break
            await asyncio.sleep(1)
        if self.open_trades:
            log.error("Still open after kill switch: %s. Check them on Deriv.",
                      ", ".join(self.open_trades))
        self.stop()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _api_id(cid: str):
    return int(cid) if cid.isdigit() else cid
