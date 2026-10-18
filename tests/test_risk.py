from datetime import datetime, timedelta, timezone

import pytest

from bot.risk import RiskManager


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def test_allows_normal_trade(cfg, clock):
    assert RiskManager(cfg, clock).check(1.0) == (True, "ok")


def test_stake_is_fixed_no_martingale(cfg, clock):
    risk = RiskManager(cfg, clock)
    ok, reason = risk.check(2.0)  # within MAX_STAKE but not the configured stake
    assert not ok and "differs" in reason
    assert not risk.check(5.0)[0]


def test_one_open_trade_at_a_time(cfg, clock):
    risk = RiskManager(cfg, clock)
    risk.record_open("1", 1.0)
    assert not risk.check(1.0)[0]
    risk.record_close("1", 0.5)
    assert risk.check(1.0)[0]


def test_daily_loss_counts_worst_case(cfg, clock):
    risk = RiskManager(cfg, clock)
    for i in range(2):
        risk.record_open(str(i), 1.0)
        risk.record_close(str(i), -1.0)
    risk.state.paused_until = None  # ignore the losing-streak pause here
    # realised -2.00 and a new 1.00 stake -> worst case -3.00, exactly the limit
    assert risk.check(1.0)[0]
    risk.record_open("x", 1.0)
    # with that trade open, another 1.00 would risk -4.00
    cfg_open = risk.check(1.0)
    assert not cfg_open[0]
    risk.record_close("x", -0.5)
    risk.state.paused_until = None
    ok, reason = risk.check(1.0)
    assert not ok and "daily loss" in reason


def test_pause_after_losing_streak(cfg, clock):
    risk = RiskManager(cfg, clock)
    for i in range(2):
        risk.record_open(str(i), 1.0)
        risk.record_close(str(i), -0.4)
    ok, reason = risk.check(1.0)
    assert not ok and "paused" in reason
    clock.now += timedelta(minutes=31)
    assert risk.check(1.0)[0]


def test_daily_trade_limit(cfg, clock):
    risk = RiskManager(cfg, clock)
    for i in range(cfg.max_trades_per_day):
        risk.record_open(str(i), 1.0)
        risk.record_close(str(i), 0.1)
    assert "trade limit" in risk.check(1.0)[1]


def test_kill_switch(cfg, clock):
    cfg.kill_switch_file.write_text("stop")
    assert "kill switch" in RiskManager(cfg, clock).check(1.0)[1]


def test_state_survives_restart_and_resets_next_day(cfg, clock):
    risk = RiskManager(cfg, clock)
    risk.record_open("1", 1.0)
    risk.record_close("1", -1.0)
    risk.record_open("2", 1.0)
    again = RiskManager(cfg, clock)
    assert again.state.realised_pnl == -1.0
    assert again.state.open_stakes == {"2": 1.0}
    clock.now += timedelta(days=1)
    tomorrow = RiskManager(cfg, clock)
    assert tomorrow.state.realised_pnl == 0.0
    assert tomorrow.state.open_stakes == {"2": 1.0}  # open trades carry over


def test_corrupt_state_refuses_to_start(cfg, clock):
    cfg.state_dir.mkdir(parents=True)
    (cfg.state_dir / "risk_state.json").write_text("{not json")
    with pytest.raises(RuntimeError):
        RiskManager(cfg, clock)
