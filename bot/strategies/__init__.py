"""Available strategies. Add a new one here to make it selectable with STRATEGY=."""

from .base import Signal, Strategy
from .demo_test import DemoTest
from .mean_revert import MeanRevert
from .trend import TrendFollow
from .watch import WatchOnly

STRATEGIES: dict[str, type[Strategy]] = {
    WatchOnly.name: WatchOnly,
    DemoTest.name: DemoTest,
    TrendFollow.name: TrendFollow,
    MeanRevert.name: MeanRevert,
}


def make_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        options = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"Unknown STRATEGY {name!r}. Options: {options}") from None


__all__ = ["Signal", "Strategy", "STRATEGIES", "make_strategy"]
