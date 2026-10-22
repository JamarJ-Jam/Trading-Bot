"""The strategy interface. A strategy only says *whether* and *which way* to
trade. It never chooses the stake or touches the account: the bot and the
risk manager own that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Signal:
    direction: Literal["UP", "DOWN"]
    reason: str


class Strategy:
    name = "base"
    # Strategies that exist only to test the plumbing set this to True,
    # and the bot refuses to run them on a real account.
    demo_only = False
    # Candle-based strategies set these so the bot can preload recent
    # candles at startup instead of waiting hours to build its own history.
    candle_seconds = 0
    warmup_candles = 0

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        """Called on every price update. Return a Signal to request a trade."""
        raise NotImplementedError

    def current_direction(self) -> str | None:
        """Optional: which way the strategy currently leans ("UP"/"DOWN"),
        used to catch up on a reversal missed while the bot was offline."""
        return None

    def status(self) -> str:
        """Optional: one plain sentence on what the strategy is waiting for,
        shown on the app's home screen."""
        return ""

    def on_trade_closed(self, profit: float) -> None:
        """Optional hook: called when one of the bot's trades closes."""
