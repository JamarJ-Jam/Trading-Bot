"""Plumbing test, demo accounts only.

Opens one minimum-size trade every few minutes, alternating Up and Down, so
you can see the whole cycle (price -> trade -> close -> log) working. It is
not a trading strategy and has no edge: expect it to lose a little over time.
"""

from __future__ import annotations

from .base import Signal, Strategy


class DemoTest(Strategy):
    name = "demo_test"
    demo_only = True

    def __init__(self, every_seconds: int = 300):
        self.every_seconds = every_seconds
        self._last_signal = 0
        self._next_direction = "UP"

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        if epoch - self._last_signal < self.every_seconds:
            return None
        self._last_signal = epoch
        direction = self._next_direction
        self._next_direction = "DOWN" if direction == "UP" else "UP"
        return Signal(direction, f"plumbing test, alternating ({direction})")
