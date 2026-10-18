"""Logging: a readable bot log plus a CSV row for every trade decision."""

from __future__ import annotations

import csv
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

TRADE_FIELDS = [
    "time_utc", "event", "account_type", "symbol", "direction", "stake",
    "multiplier", "contract_id", "buy_price", "sell_price", "profit",
    "reason", "dry_run",
]


def setup_logging(log_dir: Path, secrets: tuple[str, ...] = ()) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    redactor = _Redactor(secrets)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(redactor)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class _Redactor(logging.Filter):
    """Belt and braces: blank out the token if anything ever tries to log it."""

    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        self.secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for secret in self.secrets:
                msg = msg.replace(secret, "***")
            record.msg, record.args = msg, None
        return True


class TradeLog:
    """Appends one CSV row per event to logs/trades.csv."""

    def __init__(self, log_dir: Path):
        self.path = Path(log_dir) / "trades.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, TRADE_FIELDS).writeheader()

    def write(self, event: str, **fields) -> None:
        row = {k: "" for k in TRADE_FIELDS}
        row.update({k: v for k, v in fields.items() if k in row})
        row["time_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["event"] = event
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, TRADE_FIELDS).writerow(row)
