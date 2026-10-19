import pytest

from bot.config import Config
from bot.strategies.rules import RuleStrategy, SpecError, apply_exits, describe_spec, validate_spec
from server.lab import required_t

BASE = {"candle_seconds": 60, "entry": [{"type": "momentum", "lookback": 2}],
        "exit": {"max_trade_minutes": 30}}


def feed(strategy, closes, start=0, step=60):
    """Feed one tick per candle; the candle closes when the next one starts."""
    signals = []
    for i, price in enumerate(closes + [closes[-1]]):
        s = strategy.on_tick(price, start + i * step)
        if s:
            signals.append((i, s.direction))
    return signals


def test_validate_fills_defaults_and_rejects_unknowns():
    spec = validate_spec({"candle_seconds": 900, "entry": [{"type": "rsi"}],
                          "exit": {"take_profit": 1}})
    assert spec["entry"][0] == {"type": "rsi", "length": 14, "low": 30.0, "high": 70.0}
    assert spec["exit"]["stop_loss"] is None
    for bad in [
        {**BASE, "candle_seconds": 61},
        {**BASE, "entry": []},
        {**BASE, "entry": [{"type": "session", "start_hour": 1, "end_hour": 2}]},
        {**BASE, "entry": [{"type": "ema_cross", "fast": 30, "slow": 10}]},
        {**BASE, "entry": [{"type": "momentum", "lookback": 2, "code": "x"}]},
        {**BASE, "entry": [{"type": "momentum", "lookback": True}]},
        {**BASE, "exit": {}},
        {**BASE, "extra": 1},
    ]:
        with pytest.raises(SpecError):
            validate_spec(bad)


def test_momentum_signals_only_when_direction_changes():
    s = RuleStrategy(BASE)
    signals = feed(s, [1, 2, 3, 4, 5, 4, 3, 2, 3, 4])
    assert [d for _, d in signals] == ["UP", "DOWN", "UP"]
    assert s.current_direction() == "UP"


def test_session_filter_blocks_outside_hours():
    spec = {"candle_seconds": 3600,
            "entry": [{"type": "momentum", "lookback": 1},
                      {"type": "session", "start_hour": 7, "end_hour": 9}],
            "exit": {"exit_on_reverse": True}}
    s = RuleStrategy(spec)
    # Rising all day: only a signal once 07:00 arrives.
    signals = feed(s, [float(i) for i in range(12)], step=3600)
    assert signals and all(7 <= i < 10 for i, _ in signals)


def test_rules_must_agree():
    spec = {"candle_seconds": 60,
            "entry": [{"type": "momentum", "lookback": 1}, {"type": "breakout", "lookback": 3}],
            "exit": {"max_trade_minutes": 5}}
    s = RuleStrategy(spec)
    # Momentum is up from the second candle, but breakout only agrees once
    # a close beats the previous three.
    signals = feed(s, [5, 4, 3, 4, 6])
    assert signals == [(5, "UP")]


def test_describe_and_apply_exits():
    spec = validate_spec({"candle_seconds": 900,
                          "entry": [{"type": "breakout", "lookback": 8},
                                    {"type": "session", "start_hour": 7, "end_hour": 12}],
                          "exit": {"stop_loss": 0.5, "take_profit": 1.0}})
    assert describe_spec(spec) == "break of the 8-candle high/low and only 07:00-12:00 UTC"
    cfg = Config.from_env({"DERIV_API_TOKEN": "t", "DERIV_APP_ID": "a", "MAX_TRADE_MINUTES": "10"})
    cfg = apply_exits(cfg, spec)
    assert (cfg.stop_loss, cfg.take_profit, cfg.max_trade_minutes) == (0.5, 1.0, 0)


def test_bar_rises_with_ideas_tested():
    assert required_t(1) == 1.5
    assert required_t(10) == pytest.approx(2.0)
    assert required_t(100) == pytest.approx(2.5)
