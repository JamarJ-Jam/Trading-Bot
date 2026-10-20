import asyncio
import json

import pytest

from bot.config import Config
from bot.deriv_client import DerivError
from bot.risefall import (
    MartingaleError,
    MartingaleRunner,
    MartingaleSettings,
    contract_length,
    expiry_epoch,
    next_stake,
)


class FakeClient:
    """Answers proposals and buys at once and settles each contract
    according to `results` (True = won)."""

    def __init__(self, cfg, on_message, results, account_type="demo", payout=0.95):
        self.cfg, self.on_message = cfg, on_message
        self.results, self.account_type, self.payout = list(results), account_type, payout
        self.account = None
        self.requests = []
        self.stakes = {}
        self._closed = asyncio.Event()
        self.runner = None

    async def connect(self):
        self.account = {"account_type": self.account_type, "currency": "USD"}
        self._closed.clear()

    async def close(self):
        self._closed.set()

    async def reader(self):
        await self._closed.wait()

    async def request(self, payload):
        self.requests.append(payload)
        if "proposal" in payload and "contract_id" not in payload:
            return {"proposal": {"id": "q", "ask_price": payload["amount"]}}
        if "buy" in payload:
            cid = str(len(self.stakes) + 1)
            self.stakes[cid] = payload["price"]
            return {"buy": {"contract_id": cid, "buy_price": payload["price"]}}
        cid = str(payload["contract_id"])
        if not self.results:  # out of scripted results: stop the runner
            self.runner.stop()
            return {"proposal_open_contract": {"contract_id": cid, "is_sold": 0}}
        won = self.results.pop(0)
        stake = self.stakes[cid]
        profit = round(stake * self.payout, 2) if won else -stake
        return {"proposal_open_contract": {"contract_id": cid, "is_sold": 1, "profit": profit,
                                           "buy_price": stake}}


def make(tmp_path, results, settings=None, account_type="demo"):
    cfg = Config.from_env({"DERIV_API_TOKEN": "t", "DERIV_APP_ID": "a",
                           "LOG_DIR": str(tmp_path / "logs"),
                           "KILL_SWITCH_FILE": str(tmp_path / "STOP")})
    holder = {}

    def factory(c, on_message):
        holder["client"] = FakeClient(c, on_message, results, account_type)
        return holder["client"]

    runner = MartingaleRunner(cfg, settings or MartingaleSettings(), tmp_path / "m.json",
                              client_factory=factory)
    holder["client"].runner = runner
    return runner, holder["client"]


def run(runner):
    asyncio.run(asyncio.wait_for(runner.run(), 10))


def test_doubles_after_losses_resets_after_win_and_ends_sessions(tmp_path):
    s = MartingaleSettings(stake=1, multiplier=2, max_stake=64, profit=2, loss=100)
    runner, client = make(tmp_path, [False, False, True, True, True], s)
    run(runner)
    buys = [r["price"] for r in client.requests if "buy" in r]
    # L L W (session +0.80), W (+1.75), W (+2.70 -> ends at the $2 target).
    assert buys[:5] == [1, 2, 4, 1, 1]
    assert all(r["contract_type"] == "CALL" and r["duration_unit"] == "m"
               for r in client.requests if r.get("proposal") == 1 and "amount" in r)
    t = runner.state["totals"]
    assert t["sessions"] == 1 and t["up"] == 1 and t["trades"] == 5
    assert (tmp_path / "logs" / "martingale_sessions_live.csv").exists()
    saved = json.loads((tmp_path / "m.json").read_text())
    # The next session's first trade is still open when the fake runs out.
    assert saved["session"]["trades"] == 0 and saved["pending"]["stake"] == 1


def test_big_stake_small_thresholds_is_one_trade_sessions(tmp_path):
    s = MartingaleSettings(stake=100, max_stake=200, profit=10, loss=10, daily_loss_limit=1000)
    runner, client = make(tmp_path, [False, True, False], s)
    run(runner)
    assert [r["price"] for r in client.requests if "buy" in r][:3] == [100, 100, 100]
    assert runner.state["totals"]["sessions"] == 3


def test_refuses_a_real_account(tmp_path):
    runner, client = make(tmp_path, [True], account_type="real")
    with pytest.raises(DerivError, match="demo"):
        run(runner)
    assert not any("buy" in r for r in client.requests)


def test_config_is_forced_to_demo(tmp_path):
    cfg = Config.from_env({"DERIV_API_TOKEN": "t", "DERIV_APP_ID": "a",
                           "DERIV_ACCOUNT_TYPE": "real", "ALLOW_REAL_MONEY":
                           __import__("bot.config", fromlist=["x"]).REAL_MONEY_CONFIRMATION})
    runner = MartingaleRunner(cfg, MartingaleSettings(), tmp_path / "m.json",
                              client_factory=lambda c, h: FakeClient(c, h, []))
    assert runner.cfg.account_type == "demo" and not runner.cfg.is_real


def test_daily_loss_limit_blocks_the_next_trade(tmp_path):
    s = MartingaleSettings(stake=1, multiplier=2, max_stake=8, profit=50, loss=100,
                           daily_loss_limit=10)
    runner, client = make(tmp_path, [False, False, False, False], s)

    async def go():
        task = asyncio.create_task(runner.run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runner.status.startswith("paused"):
                break
        runner.stop()
        await task

    asyncio.run(go())
    # Lost 1+2+4 = 7; an 8 stake could reach -15, past the $10 limit.
    assert [r["price"] for r in client.requests if "buy" in r] == [1, 2, 4]
    assert runner.status.startswith("paused") or runner.status == "stopped"


def test_kill_switch_stops_before_trading(tmp_path):
    (tmp_path / "STOP").write_text("x")
    runner, client = make(tmp_path, [True])
    run(runner)
    assert not any("buy" in r for r in client.requests) and "kill switch" in runner.status


def test_settings_are_checked():
    assert next_stake(MartingaleSettings(stake=1, multiplier=2, max_stake=4), 4, False) == 1
    for bad in ({"stake": 0.1}, {"max_stake": 0.5}, {"direction": "up"},
                {"daily_loss_limit": 10, "max_stake": 64}, {"sessions": 1.5}, {"extra": 1}):
        with pytest.raises(MartingaleError):
            MartingaleSettings.from_dict(bad)


def test_contract_length_units_and_end_time():
    now = 1_700_000_000  # 22:13:20 UTC
    for unit, letter in [("ticks", "t"), ("seconds", "s"), ("minutes", "m"), ("hours", "h"), ("days", "d")]:
        n = 15 if unit == "seconds" else 5
        fields, _ = contract_length(MartingaleSettings.from_dict({"duration": n, "duration_unit": unit}), now)
        assert fields == {"duration": n, "duration_unit": letter}
    fields, seconds = contract_length(MartingaleSettings.from_dict(
        {"duration_unit": "end_time", "end_time": "22:30"}), now)
    assert fields == {"date_expiry": now + 1000} and seconds == 1000
    # A time already passed today (or under a minute away) means tomorrow.
    assert expiry_epoch("22:13", now) == now - 20 + 86400
    assert expiry_epoch("22:14", now) == now + 40 + 86400
    for bad in ({"duration": 5, "duration_unit": "seconds"}, {"duration": 11, "duration_unit": "ticks"},
                {"duration_unit": "weeks"}, {"duration_unit": "end_time", "end_time": "25:00"}):
        with pytest.raises(MartingaleError):
            MartingaleSettings.from_dict(bad)


def test_old_saved_settings_still_load():
    s = MartingaleSettings.from_dict({"duration_minutes": 3, "stake": 2})
    assert (s.duration, s.duration_unit, s.stake) == (3, "minutes", 2)
