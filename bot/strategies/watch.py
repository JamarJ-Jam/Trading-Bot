"""Default strategy: watch prices and never trade."""

from __future__ import annotations

import logging

from .base import Signal, Strategy

log = logging.getLogger(__name__)


class WatchOnly(Strategy):
    name = "watch"

    def __init__(self, report_every: int = 60):
        self.report_every = report_every
        self._last_report = 0
        self._count = 0

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        self._count += 1
        if epoch - self._last_report >= self.report_every:
            log.info("Price %s (%d updates so far). Watch-only: no trades.", price, self._count)
            self._last_report = epoch
        return None
