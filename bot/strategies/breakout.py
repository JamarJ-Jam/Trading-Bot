"""Session breakout: trade the first break of the Asian range after London opens.

Forex is usually quiet overnight (Asian hours) and picks up when London
opens. Each day this marks the highest and lowest candle close between
BO_RANGE_START and BO_RANGE_END (UTC hours). From then until BO_TRADE_END,
the first candle that closes above the range signals UP, below it DOWN.
At most one signal per day.

Settings (in .env, all optional):
  BO_CANDLE_SECONDS=900     candle size (15 minutes)
  BO_RANGE_START=0          range starts at this UTC hour
  BO_RANGE_END=7            range ends (and trading starts) at this UTC hour
  BO_TRADE_END=12           no new signals from this UTC hour

London opens at 07:00 UTC in summer and 08:00 UTC in winter. Pair it with
MAX_TRADE_MINUTES (for example 480) so trades close the same day.
"""

from __future__ import annotations

from .base import Signal, Strategy
from .trend import _env_int

DAY = 86400


class SessionBreakout(Strategy):
    name = "breakout"

    def __init__(self, candle_seconds: int | None = None, range_start: int | None = None,
                 range_end: int | None = None, trade_end: int | None = None):
        def pick(value, name, default):
            return value if value is not None else _env_int(name, default)

        self.candle_seconds = pick(candle_seconds, "BO_CANDLE_SECONDS", 900)
        self.range_start = pick(range_start, "BO_RANGE_START", 0)
        self.range_end = pick(range_end, "BO_RANGE_END", 7)
        self.trade_end = pick(trade_end, "BO_TRADE_END", 12)
        if not 0 <= self.range_start < self.range_end < self.trade_end <= 24:
            raise ValueError("Need 0 <= BO_RANGE_START < BO_RANGE_END < BO_TRADE_END <= 24")
        if self.candle_seconds <= 0 or 3600 % self.candle_seconds:
            raise ValueError("BO_CANDLE_SECONDS must divide an hour evenly (e.g. 300, 900, 3600)")
        # Enough history to rebuild today's range after a restart.
        self.warmup_candles = DAY // self.candle_seconds
        self._bucket: int | None = None
        self._close: float | None = None
        self._day: int | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._done = False

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        bucket = epoch // self.candle_seconds
        signal = None
        if self._bucket is not None and bucket != self._bucket:
            signal = self._on_candle_close(self._bucket * self.candle_seconds, self._close)
        self._bucket, self._close = bucket, price
        return signal

    def _on_candle_close(self, start: int, close: float) -> Signal | None:
        day, hour = divmod(start, DAY)
        hour //= 3600
        if day != self._day:
            self._day, self._high, self._low, self._done = day, None, None, False
        if self.range_start <= hour < self.range_end:
            self._high = close if self._high is None else max(self._high, close)
            self._low = close if self._low is None else min(self._low, close)
            return None
        if self._done or self._high is None or not self.range_end <= hour < self.trade_end:
            return None
        if close > self._high:
            self._done = True
            return Signal("UP", f"closed above the Asian range high {self._high:.5f}")
        if close < self._low:
            self._done = True
            return Signal("DOWN", f"closed below the Asian range low {self._low:.5f}")
        return None
