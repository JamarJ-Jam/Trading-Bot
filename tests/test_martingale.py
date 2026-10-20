from bot.backtest import Candle
from bot.martingale import outcomes_from_candles, run_sessions


def flips(*won):
    return [(i * 60, w) for i, w in enumerate(won)]


def test_big_stake_with_small_thresholds_is_one_trade_per_session():
    sessions = run_sessions(flips(True, False, False, True), stake=100, multiplier=2,
                            payout=0.95, profit=10, loss=10)
    assert [(s.trades, s.result, s.ended) for s in sessions] == [
        (1, 95.0, "profit target"), (1, -100.0, "loss limit"),
        (1, -100.0, "loss limit"), (1, 95.0, "profit target")]


def test_stake_doubles_after_losses_and_resets_after_a_win():
    sessions = run_sessions(flips(False, False, True, False, True, True), stake=1,
                            multiplier=2, payout=1.0, profit=3, loss=50)
    assert sessions[0].stakes == [1, 2, 4, 1, 2, 1]
    assert sessions[0].result == 3.0 and sessions[0].ended == "profit target"


def test_max_stake_sends_the_stake_back_to_the_start():
    sessions = run_sessions(flips(False, False, False, False), stake=1, multiplier=2,
                            payout=0.95, profit=10, loss=100, max_stake=4)
    assert sessions[0].stakes == [1, 2, 4, 1] and sessions[0].ended == "data ran out"


def test_outcomes_from_candles_treat_equal_as_a_loss():
    candles = [Candle(60, 1, 1, 1, 2), Candle(0, 1, 1, 1, 1), Candle(120, 2, 2, 1, 1)]
    assert outcomes_from_candles(candles) == [(0, False), (60, True), (120, False)]
    assert outcomes_from_candles(candles, "fall")[2] == (120, True)
