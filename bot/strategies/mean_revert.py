"""Mean reversion: bet that a sharp move snaps back.

Prices are grouped into candles. Bollinger bands are drawn at a moving
average plus and minus a number of standard deviations. When a candle
closes below the lower band it signals UP (expecting a bounce); above the
upper band, DOWN. It only fires on the first candle outside a band, not on
every candle while the price stays there.

Settings (in .env, all optional):
  MR_PERIOD=20              candles in the moving average
  MR_STD=2.0                band width in standard deviations
  MR_CANDLE_SECONDS=60      candle size
"""

from __future__ import annotations

import math
import os
from collections import deque

from .base import Signal, Strategy
from .trend import _env_int


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


class MeanRevert(Strategy):
    name = "mean_revert"

    def __init__(self, period: int | None = None, width: float | None = None,
                 candle_seconds: int | None = None):
        self.period = period or _env_int("MR_PERIOD", 20)
        self.width = width or _env_float("MR_STD", 2.0)
        self.candle_seconds = candle_seconds or _env_int("MR_CANDLE_SECONDS", 60)
        if self.period < 2 or self.width <= 0:
            raise ValueError("MR_PERIOD must be at least 2 and MR_STD more than 0")
        self.closes: deque[float] = deque(maxlen=self.period)
        self._bucket: int | None = None
        self._close: float | None = None
        self._outside = 0  # -1 below lower band, 1 above upper, 0 inside

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        bucket = epoch // self.candle_seconds
        signal = None
        if self._bucket is not None and bucket != self._bucket:
            signal = self._on_candle_close(self._close)
        self._bucket, self._close = bucket, price
        return signal

    def _on_candle_close(self, close: float) -> Signal | None:
        # Bands come from the candles before this one, so a spike can't
        # widen its own band.
        signal = None
        if len(self.closes) == self.period:
            mean = sum(self.closes) / self.period
            std = math.sqrt(sum((c - mean) ** 2 for c in self.closes) / self.period)
            lower, upper = mean - self.width * std, mean + self.width * std
            side = -1 if close < lower else 1 if close > upper else 0
            if side and side != self._outside:
                if side < 0:
                    signal = Signal("UP", f"closed below lower band {lower:.5f}")
                else:
                    signal = Signal("DOWN", f"closed above upper band {upper:.5f}")
            self._outside = side
        self.closes.append(close)
        return signal
