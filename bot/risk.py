"""Hard risk limits. Every trade must pass `RiskManager.check` first.

The limits are deliberately simple and conservative:

* The stake is fixed by config. Strategies cannot raise it, so there is no
  martingale (doubling up after a loss) anywhere in the bot.
* The daily loss limit counts the worst case: today's realised result,
  minus every open stake, minus the new stake. A multiplier trade can lose at
  most its stake, so the bot can never go past MAX_DAILY_LOSS even if every
  open trade lost everything at once.
* Today's numbers are saved to disk, so restarting the bot does not reset
  the daily loss limit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DayState:
    day: str
    realised_pnl: float = 0.0
    trades_opened: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    paused_until: str | None = None
    # contract_id -> stake, for trades that are still open
    open_stakes: dict[str, float] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg: Config, clock=utc_now):
        self.cfg = cfg
        self.clock = clock
        self.path = Path(cfg.state_dir) / "risk_state.json"
        self.state = self._load()

    # ---- persistence ---------------------------------------------------

    def _today(self) -> str:
        return self.clock().strftime("%Y-%m-%d")

    def _load(self) -> DayState:
        today = self._today()
        try:
            data = json.loads(self.path.read_text())
            state = DayState(**data)
        except FileNotFoundError:
            return DayState(day=today)
        except (ValueError, TypeError) as exc:
            # A corrupt state file must not silently reset the loss limit.
            raise RuntimeError(
                f"Could not read {self.path} ({exc}). Fix or delete it before starting."
            ) from exc
        if state.day != today:
            state = DayState(day=today, open_stakes=state.open_stakes)
        return state

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        tmp.replace(self.path)

    def _roll_day(self) -> None:
        today = self._today()
        if self.state.day != today:
            log.info("New trading day %s; daily counters reset", today)
            self.state = DayState(day=today, open_stakes=self.state.open_stakes)
            self._save()

    # ---- checks ----------------------------------------------------------

    def kill_switch_on(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    @property
    def open_exposure(self) -> float:
        return round(sum(self.state.open_stakes.values()), 2)

    def check(self, stake: float) -> tuple[bool, str]:
        """Return (allowed, reason). Reason explains any refusal."""
        self._roll_day()
        cfg, s = self.cfg, self.state

        if self.kill_switch_on():
            return False, f"kill switch file {cfg.kill_switch_file} exists"
        if stake <= 0 or stake > cfg.max_stake:
            return False, f"stake {stake:.2f} is outside 0..{cfg.max_stake:.2f}"
        if stake != cfg.stake:
            return False, f"stake {stake:.2f} differs from configured STAKE {cfg.stake:.2f}"
        if s.trades_opened >= cfg.max_trades_per_day:
            return False, f"daily trade limit {cfg.max_trades_per_day} reached"
        if len(s.open_stakes) >= cfg.max_open_trades:
            return False, f"{len(s.open_stakes)} trade(s) already open"
        if s.paused_until and self.clock() < datetime.fromisoformat(s.paused_until):
            return False, f"paused after losses until {s.paused_until}"
        worst_case = s.realised_pnl - self.open_exposure - stake
        if worst_case < -cfg.max_daily_loss:
            return False, (
                f"daily loss limit: worst case {worst_case:.2f} would pass "
                f"-{cfg.max_daily_loss:.2f}"
            )
        return True, "ok"

    # ---- bookkeeping -----------------------------------------------------

    def record_open(self, contract_id: str, stake: float) -> None:
        self._roll_day()
        self.state.trades_opened += 1
        self.state.open_stakes[str(contract_id)] = stake
        self._save()

    def record_close(self, contract_id: str, profit: float) -> None:
        self._roll_day()
        s = self.state
        s.open_stakes.pop(str(contract_id), None)
        s.realised_pnl = round(s.realised_pnl + profit, 2)
        if profit < 0:
            s.losses += 1
            s.consecutive_losses += 1
            if (
                self.cfg.max_consecutive_losses
                and s.consecutive_losses >= self.cfg.max_consecutive_losses
            ):
                until = self.clock() + timedelta(minutes=self.cfg.pause_minutes)
                s.paused_until = until.isoformat()
                s.consecutive_losses = 0
                log.warning("%d losses in a row; pausing until %s",
                            self.cfg.max_consecutive_losses, s.paused_until)
        else:
            s.wins += 1
            s.consecutive_losses = 0
        self._save()

    def forget_open(self, contract_id: str) -> None:
        """Drop a contract we could not track (it stays counted as a trade)."""
        self.state.open_stakes.pop(str(contract_id), None)
        self._save()

    def summary(self) -> str:
        s = self.state
        return (
            f"{s.day}: P/L {s.realised_pnl:+.2f}, trades {s.trades_opened}, "
            f"wins {s.wins}, losses {s.losses}, open {len(s.open_stakes)}"
        )
