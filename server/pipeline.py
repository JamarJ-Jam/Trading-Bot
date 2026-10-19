"""Background work for the lab: test queued ideas one at a time, refresh
paper trading every few hours, and raise an alert when an idea is ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from bot.backtest import Candle, fetch_candles
from bot.config import ConfigError

from . import lab
from .settings import Settings

log = logging.getLogger(__name__)

PAPER_REFRESH_SECONDS = 4 * 3600
HISTORY_CANDLES = {60: 20000, 300: 20000, 900: 20000, 1800: 15000, 3600: 15000,
                   14400: 5000, 86400: 1500}


class Alerts:
    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return []

    def add(self, kind: str, title: str, body: str, entry_id: str = "") -> None:
        alerts = self.all()
        alerts.insert(0, {"kind": kind, "title": title, "body": body, "entry_id": entry_id,
                          "at": int(time.time()), "read": False})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(alerts[:100], indent=1))

    def mark_read(self) -> None:
        alerts = self.all()
        for a in alerts:
            a["read"] = True
        if alerts:
            self.path.write_text(json.dumps(alerts, indent=1))


class LabPipeline:
    def __init__(self, store: lab.LabStore, settings: Settings, alerts: Alerts,
                 fetch=fetch_candles):
        self.store = store
        self.settings = settings
        self.alerts = alerts
        self.fetch = fetch
        self.wake = asyncio.Event()
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self._last_paper = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lab-pipeline")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def poke(self) -> None:
        self.wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the pipeline alive whatever one idea does
                log.exception("Lab pipeline step failed")
            try:
                await asyncio.wait_for(self.wake.wait(), 300)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def step(self, now: float | None = None) -> None:
        now = now or time.time()
        for entry in self.store.all():
            if entry["stage"] == "queued":
                await self.test(entry)
        if now - self._last_paper >= PAPER_REFRESH_SECONDS:
            self._last_paper = now
            for entry in self.store.all():
                if entry["stage"] == "paper":
                    await self.paper(entry)

    async def _candles(self, entry: dict, count: int) -> dict[str, list[Candle]]:
        gran = lab.granularity(entry)
        out = {}
        for symbol in entry["symbols"]:
            try:
                out[symbol] = await self.fetch(symbol, gran, count)
            except (OSError, RuntimeError) as exc:
                log.warning("Lab could not download %s: %s", symbol, exc)
        return out

    async def test(self, entry: dict) -> None:
        self.current = entry["id"]
        entry["stage"], entry["status"] = "backtest", "Downloading prices and backtesting"
        self.store.save(entry)
        try:
            base = self.settings.config()
            candles = await self._candles(entry, HISTORY_CANDLES.get(lab.granularity(entry), 15000))
            await asyncio.to_thread(lab.run_tests, base, entry, candles,
                                    self.store.tested_count() + 1)
        except (lab.LabError, ConfigError, ValueError) as exc:
            lab._set(entry, "failed", f"Could not test: {exc}")
        finally:
            self.current = None
        self.store.save(entry)
        log.info("Lab tested %s: %s", entry["name"], entry["status"])

    async def paper(self, entry: dict) -> None:
        gran = lab.granularity(entry)
        warmup = lab.build_strategy(entry).warmup_candles or 50
        count = warmup + int((time.time() - entry["paper_since"]) // gran) + 10
        try:
            base = self.settings.config()
            candles = await self._candles(entry, count)
            await asyncio.to_thread(lab.run_paper, base, entry, candles)
        except (lab.LabError, ConfigError, ValueError) as exc:
            log.warning("Paper update for %s failed: %s", entry["name"], exc)
            return
        self.store.save(entry)
        if entry["stage"] == "ready":
            self.alerts.add("ready", f"{entry['name']} is ready to go live",
                            "It passed all 4 readiness checks. Review it before switching "
                            "to your real account.", entry["id"])
