"""Trend following: trade in the direction of a moving-average crossover.

Prices are grouped into candles (1 minute by default) so single-tick noise
doesn't trigger trades. When the fast exponential moving average (EMA)
crosses above the slow one, it signals UP; when it crosses below, DOWN.

Settings (in .env, all optional):
  TREND_FAST=9              fast EMA length, in candles
  TREND_SLOW=21             slow EMA length, in candles
  TREND_CANDLE_SECONDS=60   candle size
"""

from __future__ import annotations

import os

from .base import Signal, Strategy


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None


class Ema:
    def __init__(self, length: int):
        self.alpha = 2 / (length + 1)
        self.value: float | None = None

    def update(self, price: float) -> float:
        self.value = price if self.value is None else self.value + self.alpha * (price - self.value)
        return self.value


class TrendFollow(Strategy):
    name = "trend"

    def __init__(self, fast: int | None = None, slow: int | None = None,
                 candle_seconds: int | None = None):
        self.fast_len = fast or _env_int("TREND_FAST", 9)
        self.slow_len = slow or _env_int("TREND_SLOW", 21)
        self.candle_seconds = candle_seconds or _env_int("TREND_CANDLE_SECONDS", 60)
        if not 1 <= self.fast_len < self.slow_len:
            raise ValueError("TREND_FAST must be at least 1 and smaller than TREND_SLOW")
        self.warmup_candles = self.slow_len + 2
        self.fast = Ema(self.fast_len)
        self.slow = Ema(self.slow_len)
        self.candles_seen = 0
        self._bucket: int | None = None
        self._close: float | None = None
        self._prev_diff: float | None = None

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        bucket = epoch // self.candle_seconds
        signal = None
        if self._bucket is not None and bucket != self._bucket:
            signal = self._on_candle_close(self._close)
        self._bucket, self._close = bucket, price
        return signal

    def _on_candle_close(self, close: float) -> Signal | None:
        self.candles_seen += 1
        diff = self.fast.update(close) - self.slow.update(close)
        prev, self._prev_diff = self._prev_diff, diff
        # Wait until the slow average has enough history to mean something.
        if prev is None or self.candles_seen <= self.slow_len:
            return None
        if prev <= 0 < diff:
            return Signal("UP", f"EMA{self.fast_len} crossed above EMA{self.slow_len}")
        if prev >= 0 > diff:
            return Signal("DOWN", f"EMA{self.fast_len} crossed below EMA{self.slow_len}")
        return None
