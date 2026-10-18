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


def test_mean_revert_fades_spikes_once():
    from bot.strategies.mean_revert import MeanRevert
    calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(30)]
    closes = calm + [95.0, 94.5, 94.0] + calm + [105.0]
    signals = feed_candles(MeanRevert(period=20, width=2.0), closes)
    # one UP on the first close below the band (not on the next two), one DOWN at the spike up
    assert [d for _, d in signals] == ["UP", "DOWN"]
    assert signals[0][0] == 30 and signals[1][0] == len(closes) - 1


def test_mean_revert_registered_and_backtests(cfg):
    from bot.strategies.mean_revert import MeanRevert
    assert isinstance(make_strategy("mean_revert"), MeanRevert)
    closes = [100 + 2 * math.sin(i / 7) + (3 if i % 97 == 0 else 0) for i in range(2000)]
    trades = simulate(cfg, MeanRevert(period=20, width=2.0), candles_from(closes), 60)
    assert trades and all(t.profit >= -cfg.stake for t in trades)


def day_closes(asia, london, seconds=900):
    """One UTC day of 15-minute closes: flat Asian range, then the given London path."""
    per_hour = 3600 // seconds
    closes = []
    for i in range(24 * per_hour):
        hour = i // per_hour
        if hour < 7:
            closes.append(asia[i % len(asia)])
        else:
            closes.append(london[min(i - 7 * per_hour, len(london) - 1)])
    return closes


def test_breakout_trades_first_break_once_per_day():
    from bot.strategies.breakout import SessionBreakout

    asia = [100.0, 100.5, 99.5]
    london = [100.0, 100.2, 101.0, 102.0, 98.0, 103.0]
    closes = day_closes(asia, london) * 2
    signals = feed_candles(SessionBreakout(), closes, seconds=900)
    # One UP per day, on the first close above 100.5 (third London candle).
    assert [d for _, d in signals] == ["UP", "UP"]
    assert signals[0][0] == 7 * 4 + 2
    assert signals[1][0] == 96 + 7 * 4 + 2


def test_breakout_ignores_breaks_outside_trade_window():
    from bot.strategies.breakout import SessionBreakout

    closes = day_closes([100.0, 100.5, 99.5], [100.0])
    closes[13 * 4] = 105.0  # 13:00 UTC, after BO_TRADE_END
    assert feed_candles(SessionBreakout(), closes, seconds=900) == []


def test_breakout_settings_validated(monkeypatch):
    monkeypatch.setenv("BO_RANGE_END", "13")
    with pytest.raises(ValueError):
        make_strategy("breakout")
    monkeypatch.delenv("BO_RANGE_END")
    monkeypatch.setenv("BO_CANDLE_SECONDS", "700")
    with pytest.raises(ValueError):
        make_strategy("breakout")


def test_backtest_cli_several_symbols(tmp_path, monkeypatch, capsys):
    closes = day_closes([100.0, 100.5, 99.5], [100.0, 101.0, 102.0, 103.0]) * 3
    candles = candles_from(closes, seconds=900)

    async def fake_fetch(symbol, granularity, count, connect=None, now=None):
        return candles

    monkeypatch.setattr(backtest, "fetch_candles", fake_fetch)
    monkeypatch.chdir(tmp_path)
    for k in ("DERIV_API_TOKEN", "DERIV_APP_ID", "STRATEGY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAX_TRADE_MINUTES", "240")
    assert backtest.main(["--strategy", "breakout", "--symbol", "frxEURUSD,frxGBPUSD",
                          "--granularity", "900"]) == 0
    out = capsys.readouterr().out
    assert "Strategy breakout on frxEURUSD" in out and "Strategy breakout on frxGBPUSD" in out
    assert "All symbols:" in out and "frxGBPUSD: 3 trades" in out
    assert (tmp_path / "logs" / "backtest_breakout_frxGBPUSD.csv").exists()


def test_parse_fred_skips_holidays():
    text = "observation_date,DEXUSEU\n2024-01-01,\n2024-01-02,1.0956\n2024-01-03,.\n2024-01-04,1.0950\n"
    candles = backtest.parse_fred(text)
    assert [c.close for c in candles] == [1.0956, 1.0950]
    assert candles[0].epoch == 1704153600 and candles[0].high == candles[0].low == 1.0956
    assert backtest.parse_fred(text, invert=True)[0].close == pytest.approx(1 / 1.0956)


def test_backtest_cli_fred_source(tmp_path, monkeypatch, capsys):
    closes = [1.0 + 0.1 * math.sin(i / 30) for i in range(800)]
    monkeypatch.setattr(backtest, "fetch_fred", lambda symbol: candles_from(closes, 86400))
    monkeypatch.chdir(tmp_path)
    for k in ("DERIV_API_TOKEN", "DERIV_APP_ID", "STRATEGY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TREND_CANDLE_SECONDS", "86400")
    monkeypatch.setenv("MAX_TRADE_MINUTES", "40320")
    assert backtest.main(["--strategy", "trend", "--source", "fred", "--granularity", "86400",
                          "--symbol", "frxEURUSD,frxUSDJPY"]) == 0
    out = capsys.readouterr().out
    assert "All symbols:" in out and "frxUSDJPY:" in out
    assert (tmp_path / "data" / "frxEURUSD_86400_fred.csv").exists()
    assert backtest.main(["--source", "fred"]) == 2


def test_simulate_exit_on_reverse_holds_until_opposite_signal(cfg):
    cfg = replace(cfg, multiplier=10, max_trade_minutes=0, max_daily_loss=5.0,
                  exit_on_reverse=True)
    closes = ([100.0] * 30 + [100 * 1.002 ** i for i in range(1, 80)]
              + [100 * 1.002 ** 79 * 0.998 ** i for i in range(1, 80)])
    trades = simulate(cfg, TrendFollow(fast=3, slow=8), candles_from(closes), 60)
    assert trades[0].direction == "UP" and trades[0].reason == "reversed"
    assert trades[0].profit > 0
    # the reversal opened a DOWN trade straight away, on the same candle
    assert len(trades) == 1  # still open at the end, so not reported


def test_simulate_rise_fall_pays_fixed_amounts(cfg):
    cfg = replace(cfg, max_trade_minutes=5, max_daily_loss=50.0, max_trades_per_day=500,
                  max_consecutive_losses=1000)
    closes = [100.0] * 30 + [100 * (1.0005 ** i) for i in range(1, 60)]
    trades = simulate(cfg, TrendFollow(fast=3, slow=8), candles_from(closes), 60, payout=0.9)
    assert trades and {t.reason for t in trades} <= {"won", "lost"}
    assert all(t.profit in (0.9, -1.0) for t in trades)
    with pytest.raises(ValueError):
        simulate(replace(cfg, max_trade_minutes=0), TrendFollow(fast=3, slow=8),
                 candles_from(closes), 60, payout=0.9)


def test_backtest_cli_rise_fall_writes_chart(tmp_path, monkeypatch, capsys):
    path = tmp_path / "prices.csv"
    closes = [100 + 2 * math.sin(i / 15) for i in range(600)]
    backtest.save_candles(path, candles_from(closes))
    monkeypatch.chdir(tmp_path)
    for k in ("DERIV_API_TOKEN", "DERIV_APP_ID", "STRATEGY"):
        monkeypatch.delenv(k, raising=False)
    assert backtest.main(["--strategy", "trend", "--file", str(path), "--payout", "0.95"]) == 0
    out = capsys.readouterr().out
    assert "Rise/Fall paying 95%" in out
    chart = tmp_path / "logs" / "backtest_trend_risefall.html"
    assert chart.exists() and "<svg" in chart.read_text()
    assert backtest.main(["--strategy", "trend", "--file", str(path), "--payout", "95"]) == 2
