"""Backtest a strategy on Deriv's past prices, with the same risk limits the
live bot uses. Nothing is traded and no token is needed.

    python -m bot.backtest                      # strategy from .env, ~2 weeks of 1-min candles
    python -m bot.backtest --strategy trend --candles 40000
    python -m bot.backtest --file data/1HZ100V_60.csv   # reuse saved prices

Multiplier P/L is modelled as stake x multiplier x price change, losing at
most the stake. Results are BEFORE Deriv's commission and spread, so real
results will be somewhat worse.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError
from .risk import RiskManager
from .strategies import Strategy, make_strategy

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
MAX_PER_REQUEST = 5000
MAX_EMPTY_DAYS = 4


@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Trade:
    direction: str
    entry_epoch: int
    entry: float
    exit_epoch: int = 0
    exit: float = 0.0
    profit: float = 0.0
    reason: str = ""


# ---- price data --------------------------------------------------------------

async def fetch_candles(symbol: str, granularity: int, count: int,
                        connect=None, now: int | None = None) -> list[Candle]:
    """Download candles from Deriv's public feed, newest first, paging back.

    Closed markets (forex at the weekend) answer "latest" with no candles,
    so an empty answer steps the window back a day at a time, up to a few
    days, before giving up.
    """
    if connect is None:
        import websockets
        connect = websockets.connect

    candles: dict[int, Candle] = {}
    end: int | str = "latest"
    empty_steps = 0
    now = now or int(time.time())
    async with connect(PUBLIC_WS, max_size=2**24) as ws:
        req_id = 0
        while len(candles) < count:
            req_id += 1
            await ws.send(json.dumps({
                "ticks_history": symbol, "style": "candles", "granularity": granularity,
                "count": min(MAX_PER_REQUEST, count - len(candles)), "end": end,
                "req_id": req_id,
            }))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("req_id") == req_id:
                    break
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", "price history request failed"))
            batch = msg.get("candles") or []
            if not batch:
                empty_steps += 1
                if empty_steps > MAX_EMPTY_DAYS:
                    break
                end = (now if end == "latest" else int(end)) - 86400
                continue
            empty_steps = 0
            for c in batch:
                candles[int(c["epoch"])] = Candle(int(c["epoch"]), float(c["open"]),
                                                  float(c["high"]), float(c["low"]),
                                                  float(c["close"]))
            earliest = min(int(c["epoch"]) for c in batch)
            if end != "latest" and earliest >= int(end):
                break
            end = earliest - 1
            print(f"  downloaded {len(candles)} candles...", file=sys.stderr)
    return sorted(candles.values(), key=lambda c: c.epoch)


def save_candles(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["epoch", "open", "high", "low", "close"])
        for c in candles:
            w.writerow([c.epoch, c.open, c.high, c.low, c.close])


def load_candles(path: Path) -> list[Candle]:
    with path.open(newline="") as f:
        return [Candle(int(r["epoch"]), float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"])) for r in csv.DictReader(f)]


# ---- long daily history from FRED ---------------------------------------------

# Deriv only serves about a year of daily candles. The US Federal Reserve
# publishes free daily exchange rates going back decades (one noon rate per
# day, so open, high, low and close are the same).
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_SERIES = {
    # symbol: (series, invert) - invert when FRED quotes the other way round
    "frxEURUSD": ("DEXUSEU", False),
    "frxGBPUSD": ("DEXUSUK", False),
    "frxAUDUSD": ("DEXUSAL", False),
    "frxNZDUSD": ("DEXUSNZ", False),
    "frxUSDJPY": ("DEXJPUS", False),
    "frxUSDCAD": ("DEXCAUS", False),
    "frxUSDCHF": ("DEXSZUS", False),
}


def parse_fred(text: str, invert: bool = False) -> list[Candle]:
    candles = []
    for row in csv.reader(text.splitlines()[1:]):
        if len(row) < 2 or row[1] in ("", "."):
            continue  # FRED leaves holidays blank
        day = datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        price = float(row[1])
        if invert:
            price = 1 / price
        candles.append(Candle(int(day.timestamp()), price, price, price, price))
    return candles


def fetch_fred(symbol: str, opener=None) -> list[Candle]:
    if symbol not in FRED_SERIES:
        raise RuntimeError(f"no FRED series for {symbol}; options: {', '.join(FRED_SERIES)}")
    series, invert = FRED_SERIES[symbol]
    if opener is None:
        from urllib.request import urlopen as opener
    with opener(FRED_URL.format(series=series), timeout=60) as resp:
        return parse_fred(resp.read().decode("utf-8"), invert)


# ---- simulation -------------------------------------------------------------

class SimClock:
    def __init__(self):
        self.now = datetime.fromtimestamp(0, timezone.utc)

    def __call__(self):
        return self.now


def _pnl(cfg: Config, trade: Trade, price: float) -> float:
    move = price / trade.entry - 1
    if trade.direction == "DOWN":
        move = -move
    return max(-cfg.stake, cfg.stake * cfg.multiplier * move)


def _check_exit(cfg: Config, trade: Trade, c: Candle) -> tuple[float, str] | None:
    worst_price = c.low if trade.direction == "UP" else c.high
    best_price = c.high if trade.direction == "UP" else c.low
    worst, best = _pnl(cfg, trade, worst_price), _pnl(cfg, trade, best_price)
    # If both a loss exit and a profit exit fall inside one candle we can't
    # tell which came first, so assume the loss (the cautious choice).
    if worst <= -cfg.stake:
        return -cfg.stake, "stopped out"
    if cfg.stop_loss and worst <= -cfg.stop_loss:
        return -cfg.stop_loss, "stop loss"
    if cfg.take_profit and best >= cfg.take_profit:
        return cfg.take_profit, "take profit"
    if cfg.max_trade_minutes and c.epoch - trade.entry_epoch >= cfg.max_trade_minutes * 60:
        return _pnl(cfg, trade, c.close), "time limit"
    return None


def simulate(cfg: Config, strategy: Strategy, candles: list[Candle],
             granularity: int, commission: float = 0.0) -> list[Trade]:
    """Replay candles through the strategy and the real RiskManager."""
    clock = SimClock()
    with tempfile.TemporaryDirectory() as tmp:
        sim_cfg = replace(cfg, state_dir=Path(tmp), kill_switch_file=Path(tmp) / "never")
        risk = RiskManager(sim_cfg, clock)
        trades: list[Trade] = []
        open_trade: Trade | None = None

        for c in candles:
            clock.now = datetime.fromtimestamp(c.epoch, timezone.utc)
            if open_trade is not None:
                result = _check_exit(cfg, open_trade, c)
                if result is not None:
                    profit, reason = result
                    profit = round(profit - commission * cfg.stake * cfg.multiplier, 2)
                    open_trade.exit_epoch, open_trade.exit = c.epoch, c.close
                    open_trade.profit, open_trade.reason = profit, reason
                    risk.record_close(str(len(trades)), profit)
                    strategy.on_trade_closed(profit)
                    trades.append(open_trade)
                    open_trade = None

            # Feed the candle close at its open time and again at its close
            # time, so candle-based strategies see the candle finish now.
            signal = strategy.on_tick(c.close, c.epoch)
            signal = strategy.on_tick(c.close, c.epoch + granularity) or signal
            if (signal is not None and open_trade is not None and cfg.exit_on_reverse
                    and signal.direction != open_trade.direction):
                profit = round(_pnl(cfg, open_trade, c.close)
                               - commission * cfg.stake * cfg.multiplier, 2)
                open_trade.exit_epoch, open_trade.exit = c.epoch + granularity, c.close
                open_trade.profit, open_trade.reason = profit, "reversed"
                risk.record_close(str(len(trades)), profit)
                strategy.on_trade_closed(profit)
                trades.append(open_trade)
                open_trade = None
            if signal is None or open_trade is not None:
                continue
            allowed, _ = risk.check(cfg.stake)
            if allowed:
                risk.record_open(str(len(trades)), cfg.stake)
                open_trade = Trade(signal.direction, c.epoch + granularity, c.close)
    return trades


# ---- report -------------------------------------------------------------------

def report(trades: list[Trade], candles: list[Candle], cfg: Config, strategy: str) -> str:
    if not candles:
        return "No price data."
    start = datetime.fromtimestamp(candles[0].epoch, timezone.utc)
    end = datetime.fromtimestamp(candles[-1].epoch, timezone.utc)
    lines = [
        f"Strategy {strategy} on {cfg.symbol}, x{cfg.multiplier}, stake {cfg.stake:.2f}",
        f"Prices {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC ({len(candles)} candles)",
    ]
    if not trades:
        return "\n".join(lines + ["No trades were taken."])
    wins = [t.profit for t in trades if t.profit > 0]
    losses = [t.profit for t in trades if t.profit <= 0]
    net = sum(t.profit for t in trades)
    equity = peak = drawdown = 0.0
    for t in trades:
        equity += t.profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = -sum(losses) / len(losses) if losses else 0.0
    breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) else 0.0
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    lines += [
        f"Trades {len(trades)}: {len(wins)} wins, {len(losses)} losses "
        f"(win rate {len(wins) / len(trades):.0%}, break-even win rate {breakeven:.0%})",
        f"Average win {avg_win:.2f}, average loss {avg_loss:.2f}",
        f"Net result {net:+.2f}, average per trade {net / len(trades):+.3f}",
        f"Worst drop from a high point {drawdown:.2f}",
        "Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())),
        "Before Deriv's commission and spread: real results will be worse.",
    ]
    return "\n".join(lines)


def write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_utc", "direction", "entry", "exit_utc", "exit", "profit", "reason"])
        for t in trades:
            w.writerow([
                datetime.fromtimestamp(t.entry_epoch, timezone.utc).isoformat(timespec="minutes"),
                t.direction, t.entry,
                datetime.fromtimestamp(t.exit_epoch, timezone.utc).isoformat(timespec="minutes"),
                t.exit, f"{t.profit:.2f}", t.reason,
            ])


def combined_report(results: list[tuple[str, list[Trade]]]) -> str:
    lines = ["All symbols:"]
    all_trades = 0
    all_net = 0.0
    for symbol, trades in results:
        net = sum(t.profit for t in trades)
        lines.append(f"  {symbol}: {len(trades)} trades, net {net:+.2f}")
        all_trades += len(trades)
        all_net += net
    lines.append(f"  Total: {all_trades} trades, net {all_net:+.2f}")
    return "\n".join(lines)


def _download(symbol: str, granularity: int, count: int,
              source: str = "deriv") -> list[Candle] | None:
    print(f"Downloading {count} candles of {symbol} from {source}...", file=sys.stderr)
    try:
        if source == "fred":
            candles = fetch_fred(symbol)[-count:]
        else:
            candles = asyncio.run(fetch_candles(symbol, granularity, count))
    except (OSError, RuntimeError) as exc:
        print(f"Could not download prices for {symbol}: {exc}", file=sys.stderr)
        return None
    if not candles:
        print(f"Deriv returned no prices for {symbol}. Check the symbol name, "
              "or try again when its market is open.", file=sys.stderr)
        return None
    suffix = "_fred" if source == "fred" else ""
    saved = Path("data") / f"{symbol}_{granularity}{suffix}.csv"
    save_candles(saved, candles)
    print(f"Saved prices to {saved} (reuse with --file)", file=sys.stderr)
    return candles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.backtest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", help="strategy name (default: STRATEGY from .env)")
    parser.add_argument("--symbol", help="symbol, or several separated by commas "
                        "(default: SYMBOL from .env)")
    parser.add_argument("--candles", type=int, default=20000,
                        help="how many candles to download (default 20000, about 2 weeks)")
    parser.add_argument("--granularity", type=int, default=60, help="candle seconds (default 60)")
    parser.add_argument("--file", type=Path, help="use a saved CSV of candles instead of downloading")
    parser.add_argument("--commission", type=float, default=0.0,
                        help="cost per trade as a fraction of stake x multiplier (default 0)")
    parser.add_argument("--source", choices=["deriv", "fred"], default="deriv",
                        help="where to get prices: deriv (default) or fred, which has "
                        "decades of daily forex rates (use with --granularity 86400)")
    args = parser.parse_args(argv)
    if args.source == "fred" and args.granularity != 86400:
        print("--source fred only has daily prices: add --granularity 86400", file=sys.stderr)
        return 2

    load_dotenv()
    # The simulated risk manager would otherwise print every losing-streak pause.
    logging.getLogger("bot.risk").setLevel(logging.ERROR)
    env = dict(os.environ)
    # The public price feed needs no login, so placeholders are fine here.
    env.setdefault("DERIV_API_TOKEN", "backtest")
    env.setdefault("DERIV_APP_ID", "backtest")
    env["DERIV_API_TOKEN"] = env["DERIV_API_TOKEN"] or "backtest"
    env["DERIV_APP_ID"] = env["DERIV_APP_ID"] or "backtest"
    env["DERIV_ACCOUNT_TYPE"] = "demo"
    if args.strategy:
        env["STRATEGY"] = args.strategy
    symbols = [x.strip() for x in (args.symbol or "").split(",") if x.strip()]
    if symbols:
        env["SYMBOL"] = symbols[0]
    try:
        cfg = Config.from_env(env)
        make_strategy(cfg.strategy)
    except (ConfigError, ValueError) as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        return 2
    symbols = symbols or [cfg.symbol]
    if args.file and len(symbols) > 1:
        print("--file works with one symbol at a time", file=sys.stderr)
        return 2

    totals: list[tuple[str, list[Trade]]] = []
    for symbol in symbols:
        sym_cfg = replace(cfg, symbol=symbol)
        if args.file:
            candles = load_candles(args.file)
        else:
            candles = _download(symbol, args.granularity, args.candles, args.source)
            if candles is None:
                if len(symbols) == 1:
                    return 1
                continue
        trades = simulate(sym_cfg, make_strategy(cfg.strategy), candles,
                          args.granularity, args.commission)
        out = Path(cfg.log_dir) / f"backtest_{cfg.strategy}_{symbol}.csv"
        write_trades(out, trades)
        if len(symbols) > 1:
            print()
        print(report(trades, candles, sym_cfg, cfg.strategy))
        print(f"Every simulated trade: {out}")
        totals.append((symbol, trades))

    if len(symbols) > 1:
        print()
        print(combined_report(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
