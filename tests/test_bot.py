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
