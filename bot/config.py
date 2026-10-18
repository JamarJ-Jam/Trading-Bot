"""Settings, read from environment variables (and a local .env file).

Nothing secret lives in code. The token is only ever read from the
environment, and it is never printed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REAL_MONEY_CONFIRMATION = "YES_I_UNDERSTAND"


class ConfigError(ValueError):
    """Raised when settings are missing or unsafe."""


def _get(env: dict, name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _money(env: dict, name: str, default: str) -> float:
    raw = _get(env, name, default)
    try:
        value = round(float(raw), 2)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} cannot be negative")
    return value


def _optional_money(env: dict, name: str) -> float | None:
    if not _get(env, name):
        return None
    value = _money(env, name, "0")
    return value or None


def _int(env: dict, name: str, default: str) -> int:
    raw = _get(env, name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} cannot be negative")
    return value


def _bool(env: dict, name: str, default: str = "false") -> bool:
    return _get(env, name, default).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_token: str = field(repr=False)
    app_id: str
    account_type: str = "demo"
    account_id: str = ""
    strategy: str = "watch"
    symbol: str = "1HZ100V"
    multiplier: int = 40
    dry_run: bool = False
    stake: float = 1.0
    max_stake: float = 2.0
    max_daily_loss: float = 5.0
    max_trades_per_day: int = 50
    max_open_trades: int = 1
    max_consecutive_losses: int = 3
    pause_minutes: int = 30
    max_trade_minutes: int = 10
    exit_on_reverse: bool = False
    take_profit: float | None = None
    stop_loss: float | None = None
    log_dir: Path = Path("logs")
    state_dir: Path = Path("state")
    kill_switch_file: Path = Path("STOP")

    @property
    def is_real(self) -> bool:
        return self.account_type == "real"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        if env is None:
            env = dict(os.environ)

        token = _get(env, "DERIV_API_TOKEN")
        app_id = _get(env, "DERIV_APP_ID")
        if not token:
            raise ConfigError("DERIV_API_TOKEN is not set. Put it in your .env file.")
        if not app_id:
            raise ConfigError("DERIV_APP_ID is not set. Put it in your .env file.")

        account_type = _get(env, "DERIV_ACCOUNT_TYPE", "demo").lower()
        if account_type not in {"demo", "real"}:
            raise ConfigError("DERIV_ACCOUNT_TYPE must be demo or real")
        if account_type == "real" and _get(env, "ALLOW_REAL_MONEY") != REAL_MONEY_CONFIRMATION:
            raise ConfigError(
                "Refusing to use a real-money account. Set "
                f"ALLOW_REAL_MONEY={REAL_MONEY_CONFIRMATION} only when you have decided to."
            )

        cfg = cls(
            api_token=token,
            app_id=app_id,
            account_type=account_type,
            account_id=_get(env, "DERIV_ACCOUNT_ID"),
            strategy=_get(env, "STRATEGY", "watch").lower(),
            symbol=_get(env, "SYMBOL", "1HZ100V"),
            multiplier=_int(env, "MULTIPLIER", "40"),
            dry_run=_bool(env, "DRY_RUN"),
            stake=_money(env, "STAKE", "1.00"),
            max_stake=_money(env, "MAX_STAKE", "2.00"),
            max_daily_loss=_money(env, "MAX_DAILY_LOSS", "5.00"),
            max_trades_per_day=_int(env, "MAX_TRADES_PER_DAY", "50"),
            max_open_trades=_int(env, "MAX_OPEN_TRADES", "1"),
            max_consecutive_losses=_int(env, "MAX_CONSECUTIVE_LOSSES", "3"),
            pause_minutes=_int(env, "PAUSE_MINUTES", "30"),
            max_trade_minutes=_int(env, "MAX_TRADE_MINUTES", "10"),
            exit_on_reverse=_bool(env, "EXIT_ON_REVERSE"),
            take_profit=_optional_money(env, "TAKE_PROFIT"),
            stop_loss=_optional_money(env, "STOP_LOSS"),
            log_dir=Path(_get(env, "LOG_DIR", "logs")),
            state_dir=Path(_get(env, "STATE_DIR", "state")),
            kill_switch_file=Path(_get(env, "KILL_SWITCH_FILE", "STOP")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.stake <= 0:
            raise ConfigError("STAKE must be more than 0")
        if self.max_stake <= 0:
            raise ConfigError("MAX_STAKE must be more than 0")
        if self.stake > self.max_stake:
            raise ConfigError("STAKE cannot be larger than MAX_STAKE")
        if self.max_daily_loss <= 0:
            raise ConfigError("MAX_DAILY_LOSS must be more than 0")
        if self.max_stake > self.max_daily_loss:
            raise ConfigError("MAX_STAKE cannot be larger than MAX_DAILY_LOSS")
        if self.max_open_trades < 1:
            raise ConfigError("MAX_OPEN_TRADES must be at least 1")
        if self.multiplier < 1:
            raise ConfigError("MULTIPLIER must be at least 1")
