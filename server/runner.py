"""Runs the trading bot inside the app server and answers the app's
start, pause and stop buttons.

* Start   clears the kill switch file and starts the bot with the current
          settings.
* Pause   stops the bot loop. Open trades stay open on Deriv with their own
          stop loss and take profit; the bot re-adopts them on the next start.
* Stop all trading  is the kill switch: it creates the STOP file, so the bot
          closes every open trade and stops, exactly like the command-line bot.

The wanted state is saved, so after a server restart the bot comes back
running only if it was running before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from bot.bot import TradingBot
from bot.config import ConfigError

from .settings import Settings

log = logging.getLogger(__name__)


class BotManager:
    def __init__(self, settings: Settings, state_dir: Path, bot_factory=TradingBot):
        self.settings = settings
        self.wanted_path = Path(state_dir) / "app_bot.json"
        self.bot_factory = bot_factory
        self.bot: TradingBot | None = None
        self.task: asyncio.Task | None = None
        self.started_at: float | None = None
        self.last_error = ""
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def _want(self, running: bool) -> None:
        self.wanted_path.parent.mkdir(parents=True, exist_ok=True)
        self.wanted_path.write_text(json.dumps({"running": running}))

    def wanted(self) -> bool:
        try:
            return bool(json.loads(self.wanted_path.read_text()).get("running"))
        except (FileNotFoundError, ValueError):
            return False

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            cfg = self.settings.config()
            strategy = self.settings.strategy(cfg)
            Path(cfg.kill_switch_file).unlink(missing_ok=True)
            self.bot = self.bot_factory(cfg, strategy=strategy)
            self.last_error = ""
            self.started_at = time.time()
            self.task = asyncio.create_task(self._run(self.bot), name="trading-bot")
            self._want(True)
            log.info("Bot started from the app")

    async def _run(self, bot: TradingBot) -> None:
        try:
            await bot.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # shown in the app instead of killing the server
            self.last_error = str(exc) or type(exc).__name__
            log.error("Bot stopped with an error: %s", self.last_error)
            self._want(False)

    async def _wait(self, timeout: float) -> None:
        if self.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout)
        except asyncio.TimeoutError:
            log.warning("Bot did not stop within %.0fs; cancelling it", timeout)
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def pause(self) -> None:
        async with self._lock:
            self._want(False)
            if self.running and self.bot:
                self.bot.stop()
                await self._wait(20)
            log.info("Bot paused from the app")

    async def stop_all(self) -> None:
        """The kill switch. Works even when the bot is not running: the STOP
        file then blocks trading until Start is pressed again."""
        async with self._lock:
            self._want(False)
            cfg = self.settings.config()
            path = Path(cfg.kill_switch_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stopped from the app\n")
            if self.running:
                await self._wait(40)
            log.warning("Stop all trading pressed in the app")

    async def restart_if_running(self) -> None:
        """Apply new settings: pause and start again if the bot was on."""
        if not self.running:
            return
        await self.pause()
        await self.start()

    async def resume_after_boot(self) -> None:
        if self.wanted():
            try:
                await self.start()
            except (ConfigError, ValueError) as exc:
                self.last_error = str(exc)
                log.error("Could not restart the bot after boot: %s", exc)

    async def shutdown(self) -> None:
        """Server is stopping: stop the loop but remember it should run."""
        if self.running and self.bot:
            self.bot.stop()
            await self._wait(20)

    def status(self) -> dict:
        bot = self.bot if self.running else None
        open_trades = []
        if bot:
            for t in bot.open_trades.values():
                open_trades.append({"contract_id": t.contract_id, "stake": t.stake,
                                    "direction": t.direction, "started_at": t.started_at})
        account = (bot.client.account or {}) if bot else {}
        return {
            "state": "running" if bot else "error" if self.last_error else "stopped",
            "started_at": self.started_at if bot else None,
            "error": self.last_error,
            "open_trades": open_trades,
            "balance": account.get("balance"),
            "currency": account.get("currency") or "USD",
            "account_id": account.get("account_id", ""),
        }
