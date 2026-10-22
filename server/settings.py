"""Bot settings changed from the app. They are saved to a JSON file and
layered over the .env values, then checked by the same `Config` rules the
command-line bot uses, so the app cannot set anything the bot would refuse.

Switching to a real-money account goes through `set_account`, which needs
the typed confirmation; the plain settings update cannot do it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from bot.config import REAL_MONEY_CONFIRMATION, Config, ConfigError
from bot.strategies import STRATEGIES, Strategy, make_strategy
from bot.strategies.rules import RuleStrategy, apply_exits

# app field -> environment variable
FIELDS = {
    "strategy": "STRATEGY",
    "symbol": "SYMBOL",
    "multiplier": "MULTIPLIER",
    "stake": "STAKE",
    "max_stake": "MAX_STAKE",
    "max_daily_loss": "MAX_DAILY_LOSS",
    "max_trades_per_day": "MAX_TRADES_PER_DAY",
    "max_consecutive_losses": "MAX_CONSECUTIVE_LOSSES",
    "stop_loss": "STOP_LOSS",
    "take_profit": "TAKE_PROFIT",
    "max_trade_minutes": "MAX_TRADE_MINUTES",
    "exit_on_reverse": "EXIT_ON_REVERSE",
}
LAB_PREFIX = "lab:"


class Settings:
    def __init__(self, path: Path, base_env: dict | None = None, lab=None):
        self.path = Path(path)
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.lab = lab
        self._lock = threading.Lock()

    def overrides(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def env(self, overrides: dict | None = None) -> dict:
        env = dict(self.base_env)
        for key, value in (self.overrides() if overrides is None else overrides).items():
            env[key] = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        return env

    def config(self, overrides: dict | None = None) -> Config:
        cfg = Config.from_env(self.env(overrides))
        entry = self.lab_entry(cfg.strategy)
        if entry and entry.get("spec"):
            cfg = apply_exits(cfg, entry["spec"])
        return cfg

    def lab_entry(self, strategy: str) -> dict | None:
        if not strategy.startswith(LAB_PREFIX) or self.lab is None:
            return None
        entry = self.lab.get(strategy[len(LAB_PREFIX):])
        if entry is None:
            raise ConfigError("the lab strategy picked in settings no longer exists")
        return entry

    def strategy(self, cfg: Config) -> Strategy:
        entry = self.lab_entry(cfg.strategy)
        if entry is None:
            return make_strategy(cfg.strategy)
        if entry.get("spec"):
            return RuleStrategy(entry["spec"], label=entry["name"])
        return make_strategy(entry["builtin"])

    def update(self, changes: dict) -> Config:
        """Apply app changes after checking them; returns the new Config."""
        unknown = set(changes) - set(FIELDS)
        if unknown:
            raise ConfigError(f"cannot change {', '.join(sorted(unknown))} here")
        with self._lock:
            data = self.overrides()
            for field, value in changes.items():
                data[FIELDS[field]] = value
            strategy = str(data.get("STRATEGY", self.base_env.get("STRATEGY", "watch")))
            if not strategy.startswith(LAB_PREFIX) and strategy not in STRATEGIES:
                raise ConfigError(f"unknown strategy {strategy!r}")
            cfg = self.config(data)
            try:
                self.strategy(cfg)  # a lab id that vanished, or bad strategy settings
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
            self._write(data)
            return cfg

    def set_account(self, account_type: str, confirm: str = "", acknowledged: bool = False) -> Config:
        with self._lock:
            data = self.overrides()
            if account_type == "real":
                if confirm.strip().upper() != "REAL" or not acknowledged:
                    raise ConfigError("type REAL and tick the box to switch to real money")
                data["DERIV_ACCOUNT_TYPE"] = "real"
                data["ALLOW_REAL_MONEY"] = REAL_MONEY_CONFIRMATION
            elif account_type == "demo":
                data["DERIV_ACCOUNT_TYPE"] = "demo"
                data["ALLOW_REAL_MONEY"] = ""
            else:
                raise ConfigError("account must be demo or real")
            cfg = self.config(data)
            self._write(data)
            return cfg

    def public(self, cfg: Config) -> dict:
        """What the app shows; never includes the token."""
        entry = None
        try:
            entry = self.lab_entry(cfg.strategy)
        except ConfigError:
            pass
        return {
            "broker": cfg.broker,
            "account_type": cfg.account_type,
            "strategy": cfg.strategy,
            "strategy_name": entry["name"] if entry else cfg.strategy,
            "lab_strategy": bool(entry),
            "symbol": cfg.symbol,
            "multiplier": cfg.multiplier,
            "stake": cfg.stake,
            "max_stake": cfg.max_stake,
            "max_daily_loss": cfg.max_daily_loss,
            "max_trades_per_day": cfg.max_trades_per_day,
            "max_consecutive_losses": cfg.max_consecutive_losses,
            "stop_loss": cfg.stop_loss,
            "take_profit": cfg.take_profit,
            "max_trade_minutes": cfg.max_trade_minutes,
            "exit_on_reverse": cfg.exit_on_reverse,
            "dry_run": cfg.dry_run,
            "builtin_strategies": sorted(STRATEGIES),
        }
