import math
from dataclasses import replace

import pytest

from bot import backtest
from bot.backtest import Candle, simulate
from bot.strategies import make_strategy
from bot.strategies.trend import TrendFollow


def feed_candles(strategy, closes, seconds=60):
    signals = []
    for i, close in enumerate(closes):
        s = strategy.on_tick(close, i * seconds)
        s = strategy.on_tick(close, (i + 1) * seconds) or s
        if s:
            signals.append((i, s.direction))
    return signals


def test_trend_signals_on_crossovers():
    closes = [100.0] * 30 + [100 + i for i in range(1, 30)] + [129 - i for i in range(1, 40)]
    signals = feed_candles(TrendFollow(fast=3, slow=8), closes)
    assert [d for _, d in signals] == ["UP", "DOWN"]
    up_at, down_at = signals[0][0], signals[1][0]
    assert 30 <= up_at < 33 and 59 <= down_at < 64


def test_trend_ignores_ticks_inside_a_candle():
    t = TrendFollow(fast=2, slow=4, candle_seconds=60)
    # many ticks within one candle never produce a signal by themselves
    assert all(t.on_tick(100 + i, 5) is None for i in range(50))


def test_trend_settings_validated(monkeypatch):
    monkeypatch.setenv("TREND_FAST", "30")
    monkeypatch.setenv("TREND_SLOW", "10")
    with pytest.raises(ValueError):
        make_strategy("trend")


def candles_from(closes, seconds=60):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(i * seconds, prev, max(prev, c), min(prev, c), c))
        prev = c
    return out


def test_simulate_profits_from_a_clean_trend(cfg):
    cfg = replace(cfg, multiplier=40, max_trade_minutes=10, max_daily_loss=3.0)
    closes = [100.0] * 30 + [100 * (1.0005 ** i) for i in range(1, 60)]
    trades = simulate(cfg, TrendFollow(fast=3, slow=8), candles_from(closes), 60)
    assert trades and trades[0].direction == "UP"
    assert trades[0].reason == "time limit"
    assert trades[0].profit > 0


def test_simulate_loss_never_exceeds_stake_and_respects_daily_limit(cfg):
    cfg = replace(cfg, multiplier=400, max_trade_minutes=0, max_consecutive_losses=0)
    closes = []
    for _ in range(20):  # repeated whipsaws: up, then crash
        closes += [100.0] * 10 + [101, 102, 103] + [95.0] * 10
    trades = simulate(cfg, TrendFollow(fast=2, slow=4), candles_from(closes), 60)
    assert trades
    assert all(t.profit >= -cfg.stake for t in trades)
    # all candles fall on one UTC day, so the daily loss limit caps the damage
    assert sum(t.profit for t in trades) >= -cfg.max_daily_loss


def test_backtest_cli_from_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "prices.csv"
    closes = [100 + 2 * math.sin(i / 15) for i in range(600)]
    backtest.save_candles(path, candles_from(closes))
    monkeypatch.chdir(tmp_path)
    for k in ("DERIV_API_TOKEN", "DERIV_APP_ID", "STRATEGY"):
        monkeypatch.delenv(k, raising=False)
    assert backtest.main(["--strategy", "trend", "--file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Strategy trend" in out and "Trades" in out
    assert (tmp_path / "logs" / "backtest_trend_1HZ100V.csv").exists()


class FakeFeed:
    """Answers ticks_history like Deriv: nothing for 'latest' while closed."""

    def __init__(self, last_open_epoch):
        self.last = last_open_epoch
        self.sent = []
        self._replies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        import json
        req = json.loads(raw)
        self.sent.append(req)
        end = self.last if req["end"] == "latest" else min(req["end"], self.last)
        if req["end"] == "latest":
            candles = []  # market closed: "latest" comes back empty
        else:
            candles = [{"epoch": end - 60 * i, "open": 1, "high": 1, "low": 1, "close": 1}
                       for i in range(req["count"])]
        self._replies.append(json.dumps({"req_id": req["req_id"], "candles": candles}))

    async def recv(self):
        return self._replies.pop(0)


async def test_fetch_steps_back_over_a_closed_market():
    now = 1_800_000_000
    feed = FakeFeed(last_open_epoch=now - 2 * 86400 - 600)  # closed for ~2 days
    candles = await backtest.fetch_candles("frxEURUSD", 60, 50,
                                           connect=lambda *a, **k: feed, now=now)
    assert len(candles) == 50
    assert feed.sent[0]["end"] == "latest"
    assert feed.sent[1]["end"] == now - 86400


async def test_fetch_gives_up_on_unknown_symbol():
    class Empty(FakeFeed):
        async def send(self, raw):
            import json
            req = json.loads(raw)
            self.sent.append(req)
            self._replies.append(json.dumps({"req_id": req["req_id"], "candles": []}))

    feed = Empty(0)
    candles = await backtest.fetch_candles("NOPE", 60, 50, connect=lambda *a, **k: feed,
                                           now=1_800_000_000)
    assert candles == []
    assert len(feed.sent) == backtest.MAX_EMPTY_DAYS + 1
