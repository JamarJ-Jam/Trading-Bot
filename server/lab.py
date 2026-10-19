"""The strategy lab: every strategy idea goes through the same pipeline
before the app will call it ready for real money.

  1. Backtest   on the older 75% of the price history, after trading costs.
  2. Unseen     the newest 25% is held back while designing. The strategy
                must also make money there. The AI never sees these numbers.
  3. Paper      it then trades live prices in simulation for at least
                PAPER_DAYS days, starting from the day it passed step 2.
  4. Ready      all four readiness checks pass; the app sends an alert.

Going live is never automatic: the person flips the account in the app.

The more ideas the lab has tested, the more convincing a backtest must be
(see `required_t`), because with enough tries some ideas pass by luck.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from bot.backtest import Candle, Trade, simulate
from bot.config import Config
from bot.strategies import Strategy, make_strategy
from bot.strategies.rules import RuleStrategy, apply_exits, describe_spec, validate_spec

HOLDOUT_FRACTION = 0.25
MIN_BACKTEST_TRADES = 30
MIN_HOLDOUT_TRADES = 10
MIN_PAPER_TRADES = 5
PAPER_DAYS = 14
DEFAULT_COMMISSION = 0.0002
BUILTINS = ("trend", "mean_revert", "breakout")

STAGES = ("queued", "backtest", "paper", "ready", "failed")

# Simulated risk managers log every losing-streak pause; keep those out of
# the live bot's log without muting the live bot's own warnings.
_simulating = contextvars.ContextVar("lab_simulating", default=False)


class _QuietSimulation(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _simulating.get()


logging.getLogger("bot.risk").addFilter(_QuietSimulation())


class LabError(ValueError):
    pass


def required_t(tested: int) -> float:
    """How strong a backtest must be, as a t-statistic of the average trade.
    1.5 for the first idea, rising by 0.5 for every tenfold more ideas tried."""
    return 1.5 + 0.5 * math.log10(max(tested, 1))


def stats(trades: list[Trade]) -> dict:
    n = len(trades)
    profits = [t.profit for t in trades]
    net = round(sum(profits), 2)
    days: dict[str, float] = {}
    for t in trades:
        day = datetime.fromtimestamp(t.entry_epoch, timezone.utc).strftime("%Y-%m-%d")
        days[day] = days.get(day, 0.0) + t.profit
    equity = peak = drawdown = 0.0
    for p in profits:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    t_stat = 0.0
    if n >= 2:
        mean = net / n
        var = sum((p - mean) ** 2 for p in profits) / (n - 1)
        if var > 0:
            t_stat = mean / math.sqrt(var / n)
    return {
        "trades": n,
        "net": net,
        "wins": sum(1 for p in profits if p > 0),
        "win_rate": round(sum(1 for p in profits if p > 0) / n, 3) if n else 0.0,
        "avg": round(net / n, 4) if n else 0.0,
        "t_stat": round(t_stat, 2),
        "worst_day": round(min(days.values()), 2) if days else 0.0,
        "max_drawdown": round(drawdown, 2),
    }


def _merge(parts: dict[str, dict]) -> dict:
    """Combine per-symbol stats into a total (t-stat from pooled trades is
    approximated by the trade-weighted average, good enough for a gate)."""
    total = sum(p["trades"] for p in parts.values())
    net = round(sum(p["net"] for p in parts.values()), 2)
    wins = sum(p["wins"] for p in parts.values())
    t_stat = (sum(p["t_stat"] * math.sqrt(p["trades"]) for p in parts.values())
              / math.sqrt(total)) if total else 0.0
    return {
        "trades": total, "net": net, "wins": wins,
        "win_rate": round(wins / total, 3) if total else 0.0,
        "avg": round(net / total, 4) if total else 0.0,
        "t_stat": round(t_stat, 2),
        "worst_day": min((p["worst_day"] for p in parts.values()), default=0.0),
        "max_drawdown": max((p["max_drawdown"] for p in parts.values()), default=0.0),
        "by_symbol": parts,
    }


def build_strategy(entry: dict) -> Strategy:
    if entry.get("spec"):
        return RuleStrategy(entry["spec"], label=entry["name"])
    return make_strategy(entry["builtin"])


def entry_config(base: Config, entry: dict, symbol: str) -> Config:
    cfg = replace(base, symbol=symbol, multiplier=int(entry.get("multiplier") or base.multiplier))
    if entry.get("spec"):
        cfg = apply_exits(cfg, entry["spec"])
    return cfg


def granularity(entry: dict) -> int:
    if entry.get("spec"):
        return entry["spec"]["candle_seconds"]
    return build_strategy(entry).candle_seconds or 60


def _run(base: Config, entry: dict, symbol: str, candles: list[Candle],
         since: int = 0, until: int | None = None) -> list[Trade]:
    token = _simulating.set(True)
    try:
        trades = simulate(entry_config(base, entry, symbol), build_strategy(entry), candles,
                          granularity(entry), float(entry.get("commission", DEFAULT_COMMISSION)))
    finally:
        _simulating.reset(token)
    return [t for t in trades if t.entry_epoch >= since and (until is None or t.entry_epoch < until)]


def run_tests(base: Config, entry: dict, candles: dict[str, list[Candle]], tested: int) -> dict:
    """Steps 1 and 2. Fills in entry["results"] and moves it to paper or failed."""
    backtest, holdout = {}, {}
    for symbol, series in candles.items():
        if len(series) < 50:
            continue
        split = series[int(len(series) * (1 - HOLDOUT_FRACTION))].epoch
        backtest[symbol] = stats(_run(base, entry, symbol, [c for c in series if c.epoch < split]))
        # Indicators warm up on the older prices; only trades opened in the
        # held-back stretch count.
        holdout[symbol] = stats(_run(base, entry, symbol, series, since=split))
    if not backtest:
        raise LabError("not enough price history to test this strategy")
    results = entry.setdefault("results", {})
    results["backtest"] = _merge(backtest)
    results["holdout"] = _merge(holdout)
    results["required_t"] = round(required_t(tested), 2)
    entry["checks"] = checks(entry, base.max_daily_loss)

    bt, ho = results["backtest"], results["holdout"]
    if not _check(entry, "backtest")["ok"]:
        if bt["trades"] < MIN_BACKTEST_TRADES:
            why = f"Only {bt['trades']} trades in the backtest; needs {MIN_BACKTEST_TRADES}"
        elif bt["net"] <= 0:
            why = (f"Lost ${-bt['net']:.2f} in the backtest after costs" if bt["net"] < 0
                   else "Made nothing in the backtest after costs")
        else:
            why = (f"Backtest profit too weak to rule out luck (score {bt['t_stat']}, "
                   f"needs {results['required_t']})")
        return _set(entry, "failed", why)
    if not _check(entry, "holdout")["ok"]:
        why = (f"Only {ho['trades']} trades on unseen data" if ho["trades"] < MIN_HOLDOUT_TRADES
               else f"Lost ${-ho['net']:.2f} on unseen data" if ho["net"] < 0
               else "Made nothing on unseen data")
        return _set(entry, "failed", why)
    entry["paper_since"] = int(time.time())
    return _set(entry, "paper", f"Paper trading live prices for {PAPER_DAYS} days")


def run_paper(base: Config, entry: dict, candles: dict[str, list[Candle]],
              now: int | None = None) -> dict:
    """Step 3: count simulated trades on live prices since paper_since."""
    now = now or int(time.time())
    since = int(entry["paper_since"])
    parts = {s: stats(_run(base, entry, s, series, since=since)) for s, series in candles.items()}
    paper = _merge(parts)
    paper["days"] = round((now - since) / 86400, 1)
    entry.setdefault("results", {})["paper"] = paper
    entry["checks"] = checks(entry, base.max_daily_loss)
    if paper["days"] < PAPER_DAYS:
        return _set(entry, "paper", f"Paper trading: day {int(paper['days']) + 1} of {PAPER_DAYS}, "
                                    f"{paper['trades']} trades, {paper['net']:+.2f}")
    if all(c["ok"] for c in entry["checks"]):
        return _set(entry, "ready", "Passed all 4 checks: ready to go live")
    if paper["trades"] < MIN_PAPER_TRADES:
        return _set(entry, "failed", f"Only {paper['trades']} paper trades in {PAPER_DAYS} days")
    if paper["net"] <= 0:
        return _set(entry, "failed", f"Lost ${-paper['net']:.2f} paper trading live prices")
    return _set(entry, "failed", "Worst day hit the daily loss limit")


def checks(entry: dict, max_daily_loss: float) -> list[dict]:
    r = entry.get("results", {})
    bt, ho, pa = r.get("backtest"), r.get("holdout"), r.get("paper")
    need_t = r.get("required_t", required_t(1))
    out = []
    if bt:
        ok = bt["trades"] >= MIN_BACKTEST_TRADES and bt["net"] > 0 and bt["t_stat"] >= need_t
        detail = f"{bt['net']:+.2f} over {bt['trades']} trades (score {bt['t_stat']}, needs {need_t})"
    else:
        ok, detail = False, "not run yet"
    out.append({"key": "backtest", "label": "Profitable in backtest after costs", "ok": ok,
                "detail": detail})
    if ho:
        ok = ho["trades"] >= MIN_HOLDOUT_TRADES and ho["net"] > 0
        detail = f"{ho['net']:+.2f} over {ho['trades']} trades"
    else:
        ok, detail = False, "not run yet"
    out.append({"key": "holdout", "label": "Holds up on unseen data", "ok": ok, "detail": detail})
    if pa:
        ok = (pa["days"] >= PAPER_DAYS and pa["trades"] >= MIN_PAPER_TRADES and pa["net"] > 0)
        detail = f"{pa['days']:g} of {PAPER_DAYS} days, {pa['trades']} trades, {pa['net']:+.2f}"
    else:
        ok, detail = False, "starts after the first two checks pass"
    out.append({"key": "paper", "label": "Profitable on live prices", "ok": ok, "detail": detail})
    worst = min((x["worst_day"] for x in (bt, ho, pa) if x), default=0.0)
    ok = bool(bt) and worst > -max_daily_loss
    out.append({"key": "worst_day", "label": "Worst day fits the loss limit", "ok": ok,
                "detail": f"worst day {worst:+.2f} of {max_daily_loss:.2f}"})
    return out


def _check(entry: dict, key: str) -> dict:
    return next(c for c in entry["checks"] if c["key"] == key)


def _set(entry: dict, stage: str, status: str) -> dict:
    entry["stage"], entry["status"] = stage, status
    entry["updated_at"] = int(time.time())
    return entry


def new_entry(name: str, source: str, symbols: list[str], *, spec: dict | None = None,
              builtin: str | None = None, multiplier: int | None = None,
              commission: float = DEFAULT_COMMISSION, note: str = "") -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise LabError("give the strategy a name")
    symbols = [s.strip() for s in symbols if s and s.strip()][:6]
    if not symbols:
        raise LabError("pick at least one market")
    for s in symbols:
        if not s.replace("_", "").isalnum() or len(s) > 20:
            raise LabError(f"{s!r} is not a market symbol")
    if spec is not None:
        spec = validate_spec(spec)
        builtin = None
    elif builtin not in BUILTINS:
        raise LabError(f"built-in strategy must be one of {', '.join(BUILTINS)}")
    if multiplier is not None and not 1 <= int(multiplier) <= 1000:
        raise LabError("multiplier must be between 1 and 1000")
    if not 0 <= commission < 0.01:
        raise LabError("commission must be a small fraction like 0.0002")
    now = int(time.time())
    return {
        "id": secrets.token_hex(4), "name": name, "source": source, "symbols": symbols,
        "spec": spec, "builtin": builtin, "multiplier": multiplier, "commission": commission,
        "description": describe_spec(spec) if spec else f"built-in {builtin} strategy",
        "note": note[:500], "stage": "queued", "status": "Waiting to be tested",
        "created_at": now, "updated_at": now, "results": {}, "checks": [],
    }


class LabStore:
    """All lab entries in one JSON file, written atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return []

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1))
        tmp.replace(self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return self._read()

    def get(self, entry_id: str) -> dict | None:
        return next((e for e in self.all() if e["id"] == entry_id), None)

    def add(self, entry: dict) -> dict:
        with self._lock:
            entries = self._read()
            entries.insert(0, entry)
            self._write(entries)
        return entry

    def save(self, entry: dict) -> None:
        with self._lock:
            entries = [entry if e["id"] == entry["id"] else e for e in self._read()]
            self._write(entries)

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._read()
            kept = [e for e in entries if e["id"] != entry_id]
            self._write(kept)
            return len(kept) != len(entries)

    def tested_count(self) -> int:
        """Ideas that reached a backtest; drives `required_t`."""
        return sum(1 for e in self.all() if e.get("results", {}).get("backtest"))
