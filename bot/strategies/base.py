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

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        """Called on every price update. Return a Signal to request a trade."""
        raise NotImplementedError

    def on_trade_closed(self, profit: float) -> None:
        """Optional hook: called when one of the bot's trades closes."""
