"""Runs the trading flow against a fake Deriv connection. No network."""

import asyncio

import pytest

from bot.bot import TradingBot
from bot.deriv_client import DerivError
from bot.risk import RiskManager
from bot.strategies import Signal, Strategy
from bot.trade_log import TradeLog


class FakeClient:
    def __init__(self, ask=1.0, fail_buy=False):
        self.account = {"account_id": "DOT1", "account_type": "demo", "currency": "USD"}
        self.sent = []
        self.ask = ask
        self.fail_buy = fail_buy

    async def request(self, payload):
        self.sent.append(payload)
        if "proposal" in payload:
            return {"msg_type": "proposal", "proposal": {"id": "P1", "ask_price": self.ask}}
        if "buy" in payload:
            if self.fail_buy:
                raise DerivError("market closed", "MarketIsClosed")
            return {"msg_type": "buy", "buy": {"contract_id": 555, "buy_price": self.ask,
                                                "start_time": 1000}}
        if "proposal_open_contract" in payload:
            return {"msg_type": "proposal_open_contract",
                    "proposal_open_contract": {"contract_id": payload["contract_id"],
                                               "is_sold": 0, "date_start": 1000}}
        if "sell" in payload:
            return {"msg_type": "sell", "sell": {}}
        if "portfolio" in payload:
            return {"msg_type": "portfolio", "portfolio": {"contracts": []}}
        if "ticks" in payload:
            return {"msg_type": "tick", "tick": {"quote": 100.0, "epoch": 1}}
        raise AssertionError(payload)

    def kinds(self):
        return [next(k for k in ("proposal_open_contract", "proposal", "buy", "sell",
                                 "portfolio", "ticks") if k in p) for p in self.sent]


class Always(Strategy):
    name = "always"

    def on_tick(self, price, epoch):
        return Signal("UP", "test")


def make_bot(cfg, client, strategy=None):
    return TradingBot(cfg, client=client, strategy=strategy or Always(),
                      risk=RiskManager(cfg), trade_log=TradeLog(cfg.log_dir))


async def test_full_trade_cycle(cfg):
    client = FakeClient()
    bot = make_bot(cfg, client)
    await bot.try_trade(Signal("UP", "test"))

    assert client.kinds() == ["proposal", "buy", "proposal_open_contract"]
    proposal = client.sent[0]
    assert proposal["contract_type"] == "MULTUP"
    assert proposal["amount"] == cfg.stake
    assert proposal["underlying_symbol"] == cfg.symbol
    assert bot.risk.state.open_stakes == {"555": 1.0}

    # second signal is refused while one trade is open
    await bot.try_trade(Signal("UP", "again"))
    assert client.kinds().count("buy") == 1

    await bot.handle_message({"msg_type": "proposal_open_contract", "proposal_open_contract": {
        "contract_id": 555, "is_sold": 1, "profit": -0.42, "status": "lost"}})
    assert bot.open_trades == {}
    assert bot.risk.state.realised_pnl == -0.42

    log = (cfg.log_dir / "trades.csv").read_text()
    assert "opened" in log and "closed" in log and "skipped" in log


async def test_dry_run_never_buys(cfg):
    from dataclasses import replace
    cfg = replace(cfg, dry_run=True)
    client = FakeClient()
    await make_bot(cfg, client).try_trade(Signal("DOWN", "test"))
    assert client.sent == []
    assert "dry_run" in (cfg.log_dir / "trades.csv").read_text()


async def test_refuses_quote_above_max_stake(cfg):
    client = FakeClient(ask=5.0)
    bot = make_bot(cfg, client)
    await bot.try_trade(Signal("UP", "test"))
    assert "buy" not in client.kinds()
    assert bot.risk.state.open_stakes == {}


async def test_buy_error_is_logged_not_raised(cfg):
    client = FakeClient(fail_buy=True)
    bot = make_bot(cfg, client)
    await bot.try_trade(Signal("UP", "test"))
    assert bot.risk.state.open_stakes == {}
    assert "market closed" in (cfg.log_dir / "trades.csv").read_text()


async def test_kill_switch_closes_trades_and_stops(cfg, monkeypatch):
    monkeypatch.setattr("bot.bot.GUARD_INTERVAL", 0.01)
    client = FakeClient()
    bot = make_bot(cfg, client)
    await bot.try_trade(Signal("UP", "test"))
    cfg.kill_switch_file.write_text("stop")

    async def close_soon():
        while "sell" not in client.kinds():
            await asyncio.sleep(0.01)
        await bot.handle_message({"msg_type": "proposal_open_contract", "proposal_open_contract": {
            "contract_id": 555, "is_sold": 1, "profit": -0.1}})

    await asyncio.wait_for(asyncio.gather(bot._guard_loop(), close_soon()), 5)
    assert bot._stop.is_set()
    assert client.sent[-1] == {"sell": 555, "price": 0}
    assert bot.open_trades == {}


async def test_demo_only_strategy_blocked_on_real(cfg):
    from dataclasses import replace
    with pytest.raises(ValueError):
        TradingBot(replace(cfg, account_type="real", strategy="demo_test"), client=FakeClient())


async def test_reconnect_adopts_open_contracts(cfg):
    client = FakeClient()
    bot = make_bot(cfg, client)
    bot.risk.record_open("777", 1.0)
    await bot._after_connect()
    assert "777" in bot.open_trades
    assert {"proposal_open_contract": 1, "contract_id": 777, "subscribe": 1} in client.sent


class CandleClient(FakeClient):
    def __init__(self, candles):
        super().__init__()
        self.candles = candles

    async def request(self, payload):
        if "ticks_history" in payload:
            self.sent.append(payload)
            return {"msg_type": "candles", "candles": self.candles}
        return await super().request(payload)


async def test_warm_up_preloads_candles_once(cfg):
    from bot.strategies.mean_revert import MeanRevert

    strategy = MeanRevert(period=3, width=1.0, candle_seconds=60)
    candles = [{"epoch": 60 * i, "close": 100.0 + i % 2} for i in range(6)]
    client = CandleClient(candles)
    bot = make_bot(cfg, client, strategy)

    await bot._warm_up()
    await bot._warm_up()

    history = [p for p in client.sent if "ticks_history" in p]
    assert len(history) == 1
    assert history[0]["granularity"] == 60 and history[0]["count"] == 6
    # Five closed candles fed; the last one is still forming.
    assert list(strategy.closes) == [100.0, 101.0, 100.0]
    assert not bot._tasks  # warm-up signals never trade


async def test_warm_up_failure_is_not_fatal(cfg):
    from bot.strategies.mean_revert import MeanRevert

    class Failing(FakeClient):
        async def request(self, payload):
            if "ticks_history" in payload:
                raise DerivError("bad granularity", "InputValidationFailed")
            return await super().request(payload)

    strategy = MeanRevert(period=3, width=1.0, candle_seconds=45)
    bot = make_bot(cfg, Failing(), strategy)
    await bot._warm_up()
    assert not strategy.closes


async def test_no_warm_up_for_tick_strategies(cfg):
    client = FakeClient()
    await make_bot(cfg, client)._warm_up()
    assert client.sent == []


async def test_reverse_closes_old_trade_then_opens_new(cfg):
    from dataclasses import replace as dc_replace
    from bot.bot import OpenTrade

    cfg = dc_replace(cfg, exit_on_reverse=True)
    client = FakeClient()
    bot = make_bot(cfg, client)
    bot.open_trades["111"] = OpenTrade("111", 1.0, "UP")
    bot.risk.record_open("111", 1.0)

    async def deriv_confirms_sale():
        while not any("sell" in p for p in client.sent):
            await asyncio.sleep(0.01)
        bot._on_contract({"contract_id": 111, "is_sold": 1, "profit": -0.2})

    bot._placing = True
    await asyncio.gather(
        bot._reverse([bot.open_trades["111"]], Signal("DOWN", "crossed")),
        deriv_confirms_sale(),
    )
    kinds = client.kinds()
    assert kinds.index("sell") < kinds.index("buy")
    assert "111" not in bot.open_trades and "555" in bot.open_trades
    assert bot.open_trades["555"].direction == "DOWN"
    assert bot._placing is False


async def test_reverse_gives_up_if_old_trade_stays_open(cfg):
    from bot.bot import OpenTrade

    client = FakeClient()
    bot = make_bot(cfg, client)
    trade = OpenTrade("111", 1.0, "UP")
    bot.open_trades["111"] = trade
    bot._placing = True
    await bot._reverse([trade], Signal("DOWN", "crossed"), wait_seconds=0.1)
    assert "buy" not in client.kinds()
    assert bot._placing is False


class Leaning(Strategy):
    name = "leaning"

    def __init__(self, side):
        self.side = side

    def on_tick(self, price, epoch):
        return None

    def current_direction(self):
        return self.side


class PortfolioClient(FakeClient):
    async def request(self, payload):
        if "portfolio" in payload:
            self.sent.append(payload)
            return {"msg_type": "portfolio", "portfolio": {"contracts": [
                {"contract_id": 111, "buy_price": 1.0, "contract_type": "MULTUP"}]}}
        return await super().request(payload)


async def test_adopted_trade_keeps_direction_and_is_reversed_if_trend_turned(cfg):
    from dataclasses import replace as dc_replace

    cfg = dc_replace(cfg, exit_on_reverse=True)
    client = PortfolioClient()
    bot = make_bot(cfg, client, Leaning("DOWN"))
    await bot._adopt_open_contracts()
    assert bot.open_trades["111"].direction == "UP"

    bot._reconcile_direction()
    while not any("sell" in p for p in client.sent):
        await asyncio.sleep(0.01)
    bot._on_contract({"contract_id": 111, "is_sold": 1, "profit": -0.1})
    await asyncio.gather(*bot._tasks)
    assert bot.open_trades["555"].direction == "DOWN"


async def test_no_reconcile_when_trend_still_agrees(cfg):
    from dataclasses import replace as dc_replace

    client = PortfolioClient()
    bot = make_bot(dc_replace(cfg, exit_on_reverse=True), client, Leaning("UP"))
    await bot._adopt_open_contracts()
    bot._reconcile_direction()
    assert not bot._tasks and "sell" not in client.kinds()


def test_trend_reports_current_direction():
    from bot.strategies.trend import TrendFollow

    t = TrendFollow(fast=2, slow=4, candle_seconds=60)
    assert t.current_direction() is None
    for i in range(10):
        t.on_tick(100 + i, i * 60)
    assert t.current_direction() == "UP"


async def test_rejoins_trend_after_deriv_expires_the_trade(cfg):
    from dataclasses import replace as dc_replace

    client = PortfolioClient()
    bot = make_bot(dc_replace(cfg, exit_on_reverse=True), client, Leaning("UP"))
    await bot._adopt_open_contracts()
    bot._on_contract({"contract_id": 111, "is_sold": 1, "is_expired": 1, "profit": 0.4})
    await asyncio.gather(*bot._tasks)
    assert bot.open_trades["555"].direction == "UP"


async def test_no_rejoin_after_stop_out(cfg):
    from dataclasses import replace as dc_replace

    client = PortfolioClient()
    bot = make_bot(dc_replace(cfg, exit_on_reverse=True), client, Leaning("UP"))
    await bot._adopt_open_contracts()
    bot._on_contract({"contract_id": 111, "is_sold": 1, "profit": -1.0})
    assert not bot._tasks and "proposal" not in client.kinds()


class Quiet(Strategy):
    name = "quiet"

    def on_tick(self, price, epoch):
        return None


class StreamingClient(FakeClient):
    """FakeClient with a connection: sells report back as closed contracts."""

    def __init__(self):
        super().__init__()
        self.bot = None

    async def connect(self):
        pass

    async def close(self):
        pass

    async def reader(self):
        await asyncio.sleep(3600)

    async def request(self, payload):
        msg = await super().request(payload)
        if "sell" in payload:
            await self.bot.handle_message({"msg_type": "proposal_open_contract",
                                           "proposal_open_contract": {
                                               "contract_id": payload["sell"], "is_sold": 1,
                                               "profit": -0.1}})
        return msg


async def run_then_ctrl_c(cfg, close_on_interrupt):
    from dataclasses import replace
    cfg = replace(cfg, max_trade_minutes=0)  # the fake contract started in 1970
    client = StreamingClient()
    risk = RiskManager(cfg)
    risk.record_open("555", 1.0)
    bot = TradingBot(cfg, client=client, strategy=Quiet(), risk=risk,
                     trade_log=TradeLog(cfg.log_dir), close_on_interrupt=close_on_interrupt)
    client.bot = bot
    task = asyncio.create_task(bot.run())
    while "ticks" not in client.kinds():
        await asyncio.sleep(0.01)
    task.cancel()  # what Ctrl+C does to the main task
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    return client, bot


async def test_ctrl_c_closes_open_trades(cfg):
    client, bot = await run_then_ctrl_c(cfg, True)
    assert "sell" in client.kinds()
    assert bot.open_trades == {}


async def test_ctrl_c_leaves_trades_open_when_not_asked_or_switched_off(cfg):
    from dataclasses import replace
    client, bot = await run_then_ctrl_c(cfg, False)
    assert "sell" not in client.kinds() and "555" in bot.open_trades
    client, bot = await run_then_ctrl_c(replace(cfg, close_on_exit=False), True)
    assert "sell" not in client.kinds()


async def test_bot_remembers_the_last_price_for_the_app(cfg):
    from bot.strategies.trend import TrendFollow
    bot = make_bot(cfg, FakeClient(), TrendFollow(fast=2, slow=3))
    assert bot.last_price is None
    bot._on_tick({"quote": 100.5, "epoch": 1000})
    assert bot.last_price == 100.5 and bot.last_tick_at is not None
    assert "Warming up: 0 of 4" in bot.strategy.status()
    for i in range(1, 8):
        bot._on_tick({"quote": 100.0 + i, "epoch": 1000 + 60 * i})
    assert "Waiting for EMA2 to cross EMA3" in bot.strategy.status()
