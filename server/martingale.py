"""Runs the demo-only Rise/Fall martingale test (bot/risefall.py) inside the
app server, next to the main bot but never at the same time as it: both
would trade the same demo account, and the main bot treats every open
contract on the account as its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from bot.risefall import MartingaleError, MartingaleRunner, MartingaleSettings

from .settings import Settings

log = logging.getLogger(__name__)


class MartingaleManager:
    def __init__(self, settings: Settings, state_dir: Path, runner_factory=MartingaleRunner):
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.settings_path = self.state_dir / "martingale_settings.json"
        self.wanted_path = self.state_dir / "martingale_wanted.json"
        self.state_path = self.state_dir / "martingale.json"
        self.runner_factory = runner_factory
        self.runner: MartingaleRunner | None = None
        self.task: asyncio.Task | None = None
        self.last_error = ""
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def options(self) -> MartingaleSettings:
        try:
            return MartingaleSettings.from_dict(json.loads(self.settings_path.read_text()))
        except (FileNotFoundError, ValueError):
            return MartingaleSettings()

    def update(self, changes: dict) -> MartingaleSettings:
        if self.running:
            raise MartingaleError("stop the test before changing its settings")
        s = MartingaleSettings.from_dict({**asdict(self.options()), **changes})
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(asdict(s)))
        return s

    def _want(self, running: bool) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.wanted_path.write_text(json.dumps({"running": running}))

    def wanted(self) -> bool:
        try:
            return bool(json.loads(self.wanted_path.read_text()).get("running"))
        except (FileNotFoundError, ValueError):
            return False

    async def start(self, fresh_count: bool = True) -> None:
        async with self._lock:
            if self.running:
                return
            cfg = self.settings.config()
            Path(cfg.kill_switch_file).unlink(missing_ok=True)
            self.runner = self.runner_factory(cfg, self.options(), self.state_path)
            if fresh_count:
                self.runner.state["sessions_done"] = 0
            self.last_error = ""
            self.task = asyncio.create_task(self._run(self.runner), name="martingale")
            self._want(True)
            log.info("Martingale test started from the app (demo)")

    async def _run(self, runner: MartingaleRunner) -> None:
        try:
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # shown in the app instead of killing the server
            self.last_error = str(exc) or type(exc).__name__
            log.error("Martingale test stopped with an error: %s", self.last_error)
        self._want(False)

    async def stop(self, timeout: float = 120) -> None:
        """Stop after the open trade settles (Rise/Fall can't be sold early
        in its last seconds, and it settles on its own within minutes)."""
        async with self._lock:
            self._want(False)
            if self.running and self.runner:
                self.runner.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), timeout)
                except asyncio.TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)

    async def shutdown(self) -> None:
        """Server is stopping: stop the loop but remember it should run."""
        if self.running and self.runner:
            self.runner.stop()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 20)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def resume_after_boot(self) -> None:
        if self.wanted():
            try:
                await self.start(fresh_count=False)
            except ValueError as exc:
                self.last_error = str(exc)

    def reset(self) -> None:
        if self.running:
            raise MartingaleError("stop the test before clearing its results")
        self.state_path.unlink(missing_ok=True)

    def status(self) -> dict:
        if self.runner:
            summary = self.runner.summary()
        else:
            try:
                saved = json.loads(self.state_path.read_text())
            except (FileNotFoundError, ValueError):
                saved = {}
            summary = {"status": "stopped", "today": saved.get("day_pnl", 0.0),
                       "session": saved.get("session"), "pending": saved.get("pending"),
                       "totals": saved.get("totals") or {"sessions": 0, "up": 0, "down": 0,
                                                         "result": 0.0, "trades": 0},
                       "recent": saved.get("recent") or []}
        if not self.running:
            summary["status"] = (f"stopped with an error: {self.last_error}" if self.last_error
                                 else summary["status"] if summary["status"].startswith("stopped")
                                 else "stopped")
        return {"running": self.running, "settings": asdict(self.options()), **summary}
