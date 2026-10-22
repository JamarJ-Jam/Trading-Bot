"""Runs the MetaTrader 5 backend against a fake MT5 terminal. No Windows needed."""

import asyncio
from collections import namedtuple
from dataclasses import replace

import pytest

from bot.bot import TradingBot
from bot.config import Config, ConfigError
from bot.mt5_client import Mt5Client, Mt5Error
from bot.risk import RiskManager
from bot.strategies import Signal, Strategy
from bot.trade_log import TradeLog

Account = namedtuple("Account", "login trade_mode margin_mode currency balance server")
Terminal = namedtuple("Terminal", "connected trade_allowed")
SymbolInfo = namedtuple("SymbolInfo", "volume_min volume_max volume_step point digits "
                                      "trade_tick_size trade_stops_level filling_mode")
Tick = namedtuple("Tick", "time time_msc bid ask")
Position = namedtuple("Position", "ticket time type magic volume price_open sl tp profit symbol")
Deal = namedtuple("Deal", "ticket position_id entry reason price profit commission swap fee")
Result = namedtuple("Result", "retcode order deal price volume comment")
SYMBOL = "Volatility 100 (1s) Index"


class FakeMt5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, ORDER_TIME_GTC = 1, 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_CLIENT, DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP = 0, 3, 4, 5
    TIMEFRAME_M1, TIMEFRAME_M15 = 1, 15

    def __init__(self, trade_mode=0, margin_mode=2, now=1_000_000):
        self.account = Account(4242, trade_mode, margin_mode, "USD", 10000.0, "Deriv-Demo")
        self.info = SymbolInfo(0.5, 100.0, 0.01, 0.01, 2, 0.01, 0, 1)
        self.now = now
        self.bid, self.ask = 1000.00, 1000.10
        self.positions: dict[int, Position] = {}
        self.deals: list[Deal] = []
        self.orders = []
        self.init_kwargs = None
        self.connected = True
        self._next = 100

    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        return True

    def shutdown(self):
        return True

    def last_error(self):
        return (1, "Success")

    def account_info(self):
        return self.account

    def terminal_info(self):
        return Terminal(self.connected, True)

    def symbol_select(self, symbol, enable):
        return symbol == SYMBOL

    def symbol_info(self, symbol):
        return self.info

    def symbol_info_tick(self, symbol):
        return Tick(self.now, self.now * 1000, self.bid, self.ask)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return [{"time": self.now - 60 * i, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
                for i in range(count, 0, -1)]

    def order_calc_profit(self, kind, symbol, volume, open_price, close_price):
        move = close_price - open_price
        return round((move if kind == self.ORDER_TYPE_BUY else -move) * volume, 2)

    def order_send(self, request):
        self.orders.append(request)
        self._next += 1
        ticket = self._next
        if "position" in request:
            pos = self.positions.pop(request["position"])
            self._close(pos, request["price"], self.DEAL_REASON_EXPERT)
            return Result(10009, ticket, ticket, request["price"], pos.volume, "done")
        kind = 0 if request["type"] == self.ORDER_TYPE_BUY else 1
        self.positions[ticket] = Position(ticket, self.now, kind, request["magic"],
                                          request["volume"], request["price"], request["sl"],
                                          request["tp"], 0.0, request["symbol"])
        self.deals.append(Deal(ticket, ticket, self.DEAL_ENTRY_IN, 0, request["price"],
                               0.0, -0.05, 0.0, 0.0))
        return Result(10009, ticket, ticket, request["price"], request["volume"], "done")

    def _close(self, pos, price, reason):
        kind = self.ORDER_TYPE_BUY if pos.type == 0 else self.ORDER_TYPE_SELL
        profit = self.order_calc_profit(kind, pos.symbol, pos.volume, pos.price_open, price)
        self.deals.append(Deal(len(self.deals) + 1, pos.ticket, self.DEAL_ENTRY_OUT, reason,
                               price, profit, 0.0, 0.0, 0.0))

    def hit_stop_loss(self, ticket):
        pos = self.positions.pop(ticket)
        self._close(pos, pos.sl, self.DEAL_REASON_SL)

    def positions_get(self, ticket=None):
        if ticket is None:
            return tuple(self.positions.values())
        return (self.positions[ticket],) if ticket in self.positions else ()

    def history_deals_get(self, ticket=None, position=None):
        return tuple(d for d in self.deals
                     if (ticket is None or d.ticket == ticket)
                     and (position is None or d.position_id == position))


@pytest.fixture
def mcfg(cfg):
    return replace(cfg, broker="mt5", symbol=SYMBOL, api_token="", app_id="")


async def connected(mcfg, fake=None, on_message=None):
    async def ignore(_msg):
        return None

    fake = fake or FakeMt5()
    client = Mt5Client(mcfg, on_message or ignore, mt5_module=fake)
    await client.connect()
    return client, fake


def test_config_for_mt5():
    cfg = Config.from_env({"BROKER": "mt5", "SYMBOL": "EURUSD"})
    assert cfg.is_mt5 and cfg.api_token == "" and cfg.mt5_login == ""
    with pytest.raises(ConfigError, match="SYMBOL"):
        Config.from_env({"BROKER": "mt5"})
    assert Config.from_env({"BROKER": "mt5"}, app=True).symbol == ""
    assert Config.from_env({"DERIV_API_TOKEN": "t", "DERIV_APP_ID": "a"}).symbol == "1HZ100V"
    with pytest.raises(ConfigError, match="MT5_PASSWORD"):
        Config.from_env({"BROKER": "mt5", "SYMBOL": "EURUSD", "MT5_LOGIN": "123"})
    with pytest.raises(ConfigError, match="BROKER"):
        Config.from_env({"BROKER": "mt4", "SYMBOL": "EURUSD"})
    full = Config.from_env({"BROKER": "mt5", "SYMBOL": "EURUSD", "MT5_LOGIN": "123",
                            "MT5_PASSWORD": "pw-secret", "MT5_SERVER": "Deriv-Demo"})
    assert "pw-secret" not in repr(full)


async def test_connect_uses_terminal_login_and_demo(mcfg):
    client, fake = await connected(mcfg)
    assert fake.init_kwargs == {}
    assert client.account["account_type"] == "demo"
    assert client.account["account_id"] == "4242"


async def test_refuses_real_account_when_set_to_demo(mcfg):
    with pytest.raises(Mt5Error, match="not a demo account") as err:
        await connected(mcfg, FakeMt5(trade_mode=2))
    assert err.value.fatal


async def test_refuses_several_trades_on_netting_account(mcfg):
    with pytest.raises(Mt5Error, match="MAX_OPEN_TRADES"):
        await connected(replace(mcfg, max_open_trades=2), FakeMt5(margin_mode=0))


async def test_unknown_symbol_is_fatal(mcfg):
    with pytest.raises(Mt5Error, match="Market Watch"):
        await connected(replace(mcfg, symbol="1HZ100V"))


async def test_proposal_puts_stop_loss_at_the_stake(mcfg):
    client, fake = await connected(mcfg)
    up = (await client.request({"proposal": 1, "amount": 1.0, "contract_type": "MULTUP",
                                "limit_order": {"take_profit": 3.0}}))["proposal"]
    # 0.5 lots, 1.00 per 2.00 of price: stop 2.00 below the ask, target 6.00 above
    assert up["volume"] == 0.5 and up["ask_price"] == 1.0
    assert up["stop_loss_price"] == pytest.approx(998.10)
    assert up["take_profit_price"] == pytest.approx(1006.10)
    down = (await client.request({"proposal": 1, "amount": 1.0, "contract_type": "MULTDOWN",
                                  "limit_order": {"stop_loss": 0.5}}))["proposal"]
    assert down["stop_loss_price"] == pytest.approx(1001.00) and down["ask_price"] == 0.5


async def test_stake_too_small_for_lot_is_refused(mcfg):
    client, fake = await connected(replace(mcfg, mt5_volume=50.0))
    with pytest.raises(Mt5Error, match="too close"):
        await client.request({"proposal": 1, "amount": 1.0, "contract_type": "MULTUP"})


class Never(Strategy):
    name = "never"


async def test_full_trade_cycle_through_the_bot(mcfg):
    fake = FakeMt5()
    bot = TradingBot(mcfg, strategy=Never(), risk=RiskManager(mcfg),
                     trade_log=TradeLog(mcfg.log_dir))
    bot.client.mt5 = fake
    await bot.client.connect()
    await bot.try_trade(Signal("UP", "test"))

    (order,) = fake.orders
    assert order["sl"] == pytest.approx(998.10) and order["magic"] == mcfg.mt5_magic
    (ticket,) = fake.positions
    assert bot.risk.state.open_stakes == {str(ticket): 1.0}
    assert bot.open_trades[str(ticket)].direction == "UP"

    fake.hit_stop_loss(ticket)
    reader = asyncio.create_task(bot.client.reader())
    for _ in range(50):
        if not bot.open_trades:
            break
        await asyncio.sleep(0.05)
    await bot.client.close()
    await asyncio.wait_for(reader, 2)

    assert bot.open_trades == {}
    assert bot.risk.state.realised_pnl == pytest.approx(-1.05)  # stop loss plus commission
    log = (mcfg.log_dir / "trades.csv").read_text()
    assert "stop loss" in log


async def test_sell_closes_the_position(mcfg):
    client, fake = await connected(mcfg)
    quote = (await client.request({"proposal": 1, "amount": 1.0,
                                   "contract_type": "MULTDOWN"}))["proposal"]
    ticket = (await client.request({"buy": quote["id"], "price": 1.0}))["buy"]["contract_id"]
    fake.ask = 999.10
    await client.request({"sell": ticket, "price": 0})
    assert fake.positions == {}
    closed = (await client.request({"proposal_open_contract": 1,
                                    "contract_id": ticket}))["proposal_open_contract"]
    assert closed["is_sold"] == 1 and closed["profit"] == pytest.approx(0.40)


async def test_portfolio_only_shows_the_bots_positions(mcfg):
    client, fake = await connected(mcfg)
    fake.positions[7] = Position(7, fake.now, 0, 0, 1.0, 1000.0, 0.0, 0.0, 0.0, SYMBOL)
    fake.positions[8] = Position(8, fake.now, 1, mcfg.mt5_magic, 0.5, 1000.0, 1002.0, 0.0,
                                 0.0, SYMBOL)
    contracts = (await client.request({"portfolio": 1}))["portfolio"]["contracts"]
    assert contracts == [{"contract_id": 8, "buy_price": 1.0, "contract_type": "MULTDOWN"}]


async def test_ticks_and_candles_are_in_utc(mcfg):
    client, fake = await connected(replace(mcfg, mt5_utc_offset_hours=2))
    tick = (await client.request({"ticks": SYMBOL, "subscribe": 1}))["tick"]
    assert tick["quote"] == 1000.00 and tick["epoch"] == fake.now - 7200
    candles = (await client.request({"ticks_history": SYMBOL, "style": "candles",
                                     "granularity": 60, "count": 3}))["candles"]
    assert len(candles) == 3 and candles[-1]["epoch"] == fake.now - 60 - 7200


async def test_reader_raises_when_terminal_disconnects(mcfg):
    from bot.deriv_client import NotConnected

    client, fake = await connected(mcfg)
    fake.connected = False
    with pytest.raises(NotConnected):
        await asyncio.wait_for(client.reader(), 2)
