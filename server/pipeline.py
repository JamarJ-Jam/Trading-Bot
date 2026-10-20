"""Background work for the lab: test queued ideas one at a time, refresh
paper trading every few hours, and raise an alert when an idea is ready.

With auto ideas on, it also keeps asking the AI for new ideas while fewer
than QUEUE_SIZE are waiting to be tested (within the AI daily cap), and it
deletes failed ideas once they are older than the chosen number of days.
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
from .ai import AIError, IdeaGenerator
from .settings import LAB_PREFIX, Settings

log = logging.getLogger(__name__)

PAPER_REFRESH_SECONDS = 4 * 3600
QUEUE_SIZE = 5               # auto ideas pause while this many wait to be tested
AI_RETRY_SECONDS = 30 * 60   # after an AI error, wait this long before asking again
DELETE_FAILED_CHOICES = (0, 1, 3, 7, 30)  # days; 0 keeps failed ideas
LAB_OPTION_DEFAULTS = {"auto_ideas": False, "delete_failed_days": 3}
MAX_AI_IDEAS_PER_DAY = 500
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


class LabOptions:
    """The lab's own switches, changed from the Lab screen."""

    def __init__(self, path: Path, ai_ideas_per_day: int = 20):
        self.path = Path(path)
        self.defaults = {**LAB_OPTION_DEFAULTS, "ai_ideas_per_day": ai_ideas_per_day}

    def get(self) -> dict:
        try:
            saved = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            saved = {}
        return {**self.defaults, **{k: v for k, v in saved.items() if k in self.defaults}}

    def update(self, changes: dict) -> dict:
        data = self.get()
        if "auto_ideas" in changes:
            if not isinstance(changes["auto_ideas"], bool):
                raise ValueError("auto_ideas must be true or false")
            data["auto_ideas"] = changes["auto_ideas"]
        if "delete_failed_days" in changes:
            if changes["delete_failed_days"] not in DELETE_FAILED_CHOICES:
                raise ValueError("delete_failed_days must be one of "
                                 + ", ".join(map(str, DELETE_FAILED_CHOICES)))
            data["delete_failed_days"] = changes["delete_failed_days"]
        if "ai_ideas_per_day" in changes:
            value = changes["ai_ideas_per_day"]
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 1 <= value <= MAX_AI_IDEAS_PER_DAY:
                raise ValueError(f"AI ideas per day must be 1 to {MAX_AI_IDEAS_PER_DAY}")
            data["ai_ideas_per_day"] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        return data


class LabPipeline:
    def __init__(self, store: lab.LabStore, settings: Settings, alerts: Alerts,
                 fetch=fetch_candles, ai: IdeaGenerator | None = None,
                 symbols: list[str] | None = None, options: LabOptions | None = None):
        self.store = store
        self.settings = settings
        self.alerts = alerts
        self.fetch = fetch
        self.ai = ai
        self.symbols = symbols or []
        self.options = options
        self.wake = asyncio.Event()
        self.current: str | None = None
        self.auto_status = "Off"
        self._task: asyncio.Task | None = None
        self._last_paper = 0.0
        self._ai_retry_at = 0.0

    def start(self) -> None:
        # A backtest cut off by a restart starts again from the top.
        for entry in self.store.all():
            if entry["stage"] == "backtest":
                entry["stage"], entry["status"] = "queued", "Waiting to be tested"
                self.store.save(entry)
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
        self.delete_old_failures(now)
        await self.top_up(now)
        for entry in self.store.all():
            if entry["stage"] == "queued" and self.store.get(entry["id"]):
                await self.test(entry)
        if now - self._last_paper >= PAPER_REFRESH_SECONDS:
            self._last_paper = now
            for entry in self.store.all():
                if entry["stage"] == "paper":
                    await self.paper(entry)

    def waiting(self) -> int:
        return sum(1 for e in self.store.all() if e["stage"] in ("queued", "backtest"))

    def _in_use(self) -> str:
        try:
            strategy = self.settings.config().strategy
        except (ConfigError, ValueError):
            return ""
        return strategy[len(LAB_PREFIX):] if strategy.startswith(LAB_PREFIX) else ""

    async def add_ai_ideas(self, count: int) -> tuple[list[dict], list[str]]:
        """Ask the AI for ideas and queue the valid ones (raises AIError)."""
        cfg = self.settings.config()
        ideas, rejected = await self.ai.ideas(count, self.symbols,
                                              self.store.all() + self.store.history(),
                                              cfg.max_daily_loss)
        added = [self.store.add(lab.new_entry(i["name"], "ai", i["symbols"], spec=i["spec"],
                                              note=i["why"])) for i in ideas]
        self.poke()
        return added, rejected

    async def top_up(self, now: float) -> None:
        """Auto ideas: refill the queue up to QUEUE_SIZE."""
        if not (self.options and self.options.get()["auto_ideas"]):
            self.auto_status = "Off"
            return
        if not (self.ai and self.ai.configured):
            self.auto_status = "Needs OPENROUTER_API_KEY on the server"
            return
        room = QUEUE_SIZE - self.waiting()
        if room <= 0:
            self.auto_status = f"Paused: {QUEUE_SIZE} ideas are waiting to be tested"
            return
        if not self.ai.remaining_today():
            self.auto_status = "Paused: today's AI limit is used up"
            return
        if now < self._ai_retry_at:
            return  # keeps the error message until the retry
        try:
            added, rejected = await self.add_ai_ideas(room)
        except (AIError, ConfigError, ValueError) as exc:
            self._ai_retry_at = now + AI_RETRY_SECONDS
            self.auto_status = f"AI error, trying again in 30 minutes: {exc}"
            log.warning("Auto ideas: %s", exc)
            return
        self.auto_status = (f"Added {len(added)} idea{'s' if len(added) != 1 else ''}"
                            + (f", {len(rejected)} rejected" if rejected else ""))

    def delete_old_failures(self, now: float) -> int:
        days = self.options.get()["delete_failed_days"] if self.options else 0
        if not days:
            return 0
        keep = self._in_use()
        gone = self.store.delete_where(
            lambda e: e["stage"] == "failed" and e["id"] != keep
            and now - e.get("updated_at", now) >= days * 86400)
        if gone:
            log.info("Lab deleted %d failed idea(s) older than %d days", gone, days)
        return gone

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
