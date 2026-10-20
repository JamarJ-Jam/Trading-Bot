"""Replay Deriv's "Automate Rise/Fall" martingale on real price history.

Demo-only study tool: it never places a trade. It answers "how often does a
session end up, and what does a bad day cost?" for the same settings as
Deriv's Automate screen:

    python -m bot.martingale --stake 100 --multiplier 2 --profit 10 --loss 10

Rules (as Deriv's quick strategy describes them):
  * Each trade is a Rise (or Fall) contract on the next candle; a win pays
    stake x payout, a loss costs the stake. Equal prices lose, as with
    "Allow equals" off.
  * After a loss the stake is multiplied; after a win it goes back to the
    starting stake. If the next stake would be above --max-stake, it goes
    back to the starting stake.
  * A session stops once its profit reaches --profit or its loss reaches
    --loss; the next session starts straight after.

Volatility indices are random by design, so prices only decide which
trades win; with --sim the outcomes are coin flips instead of downloaded
prices, which is the same thing in a long run.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backtest import Candle, fetch_candles, load_candles, save_candles


@dataclass
class Session:
    start: int
    trades: int = 0
    result: float = 0.0
    biggest_stake: float = 0.0
    worst_drawdown: float = 0.0
    ended: str = "data ran out"
    stakes: list[float] = field(default_factory=list)

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.start, timezone.utc).strftime("%Y-%m-%d")


def outcomes_from_candles(candles: list[Candle], direction: str = "rise") -> list[tuple[int, bool]]:
    """(time, won) for a contract over each candle, in time order."""
    out = []
    for c in sorted(candles, key=lambda c: c.epoch):
        won = c.close > c.open if direction == "rise" else c.close < c.open
        out.append((c.epoch, won))
    return out


def run_sessions(outcomes: list[tuple[int, bool]], *, stake: float, multiplier: float,
                 payout: float, profit: float, loss: float,
                 max_stake: float | None = None) -> list[Session]:
    sessions: list[Session] = []
    current: Session | None = None
    bet = stake
    for when, won in outcomes:
        if current is None:
            current, bet = Session(start=when), stake
        current.trades += 1
        current.stakes.append(bet)
        current.biggest_stake = max(current.biggest_stake, bet)
        current.result = round(current.result + (bet * payout if won else -bet), 2)
        current.worst_drawdown = min(current.worst_drawdown, current.result)
        if current.result >= profit:
            current.ended = "profit target"
        elif current.result <= -loss:
            current.ended = "loss limit"
        else:
            bet = stake if won else bet * multiplier
            if max_stake is not None and bet > max_stake:
                bet = stake
            continue
        sessions.append(current)
        current = None
    if current is not None:
        sessions.append(current)
    return sessions


def report(sessions: list[Session], args) -> str:
    done = [s for s in sessions if s.ended != "data ran out"]
    if not done:
        return "Not enough prices for a single finished session."
    wins = [s for s in done if s.ended == "profit target"]
    losses = [s for s in done if s.ended == "loss limit"]
    total = round(sum(s.result for s in done), 2)
    days: dict[str, float] = {}
    for s in done:
        days[s.day] = round(days.get(s.day, 0.0) + s.result, 2)
    worst = min(done, key=lambda s: s.result)
    lines = [
        f"Martingale on {args.symbol} {args.direction}, {args.granularity // 60}-minute contracts "
        f"paying {args.payout:.0%}",
        f"Start stake ${args.stake:g}, x{args.multiplier:g} after a loss, max stake "
        f"{'none' if args.max_stake is None else f'${args.max_stake:g}'}, "
        f"stop at +${args.profit:g} or -${args.loss:g}",
        "",
        f"Sessions:            {len(done)}",
        f"Ended in profit:     {len(wins)} ({len(wins) / len(done):.0%}), "
        f"average {sum(s.result for s in wins) / max(len(wins), 1):+.2f}",
        f"Ended at loss limit: {len(losses)} ({len(losses) / len(done):.0%}), "
        f"average {sum(s.result for s in losses) / max(len(losses), 1):+.2f}",
        f"Average per session: {total / len(done):+.2f}",
        f"Total:               {total:+.2f} over {sum(s.trades for s in done)} trades",
        f"Worst session:       {worst.result:+.2f} ({worst.trades} trades, "
        f"biggest stake ${worst.biggest_stake:g})",
        f"Biggest stake used:  ${max(s.biggest_stake for s in done):g}",
        f"Trades per session:  {sum(s.trades for s in done) / len(done):.1f} on average",
    ]
    if len(days) > 1:
        values = sorted(days.values())
        lines += [
            "",
            f"Running sessions back to back all day ({len(days)} days):",
            f"  Days up:   {sum(1 for v in values if v > 0)} of {len(values)}",
            f"  Best day:  {values[-1]:+.2f}",
            f"  Worst day: {values[0]:+.2f}",
        ]
    if all(s.trades == 1 for s in done):
        lines += ["", "Every session was a single trade: one win already beats the profit "
                  "target and one loss already passes the loss limit, so the martingale "
                  "never gets to double. Each session is a coin flip that pays "
                  f"+{args.stake * args.payout:g} or costs -{args.stake:g}."]
    return "\n".join(lines)


def save_sessions(path: Path, sessions: list[Session]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_utc", "trades", "result", "ended", "biggest_stake", "worst_drawdown", "stakes"])
        for s in sessions:
            w.writerow([datetime.fromtimestamp(s.start, timezone.utc).isoformat(), s.trades,
                        f"{s.result:.2f}", s.ended, f"{s.biggest_stake:g}",
                        f"{s.worst_drawdown:.2f}", " ".join(f"{x:g}" for x in s.stakes)])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot.martingale", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="1HZ100V", help="default 1HZ100V, Volatility 100 (1s)")
    p.add_argument("--direction", choices=["rise", "fall"], default="rise")
    p.add_argument("--stake", type=float, default=100)
    p.add_argument("--multiplier", type=float, default=2, help="stake x this after a loss")
    p.add_argument("--max-stake", type=float, help="back to the start stake above this")
    p.add_argument("--profit", type=float, default=10, help="session profit threshold, $")
    p.add_argument("--loss", type=float, default=10, help="session loss threshold, $")
    p.add_argument("--payout", type=float, default=0.95,
                   help="profit on a win as a fraction of stake (default 0.95; Deriv shows "
                   "the exact payout on the contract)")
    p.add_argument("--granularity", type=int, default=60, help="contract length in seconds")
    p.add_argument("--candles", type=int, default=30000, help="prices to download (~3 weeks)")
    p.add_argument("--file", type=Path, help="use a saved CSV of candles instead")
    p.add_argument("--sim", type=int, metavar="TRADES",
                   help="use this many coin-flip trades instead of prices")
    p.add_argument("--seed", type=int, help="random seed for --sim")
    args = p.parse_args(argv)
    if args.stake <= 0 or args.multiplier < 1 or args.profit <= 0 or args.loss <= 0:
        print("stake, profit and loss must be above 0 and the multiplier at least 1",
              file=sys.stderr)
        return 2

    if args.sim:
        rng = random.Random(args.seed)
        start = 1_700_000_000
        outcomes = [(start + i * args.granularity, rng.random() < 0.5) for i in range(args.sim)]
    else:
        if args.file:
            candles = load_candles(args.file)
        else:
            print(f"Downloading {args.candles} candles of {args.symbol}...", file=sys.stderr)
            try:
                candles = asyncio.run(fetch_candles(args.symbol, args.granularity, args.candles))
            except (OSError, RuntimeError) as exc:
                print(f"Could not download prices: {exc}", file=sys.stderr)
                return 1
            save_candles(Path("data") / f"{args.symbol}_{args.granularity}.csv", candles)
        outcomes = outcomes_from_candles(candles, args.direction)

    sessions = run_sessions(outcomes, stake=args.stake, multiplier=args.multiplier,
                            payout=args.payout, profit=args.profit, loss=args.loss,
                            max_stake=args.max_stake)
    out = Path("logs") / "martingale_sessions.csv"
    save_sessions(out, sessions)
    print(report(sessions, args))
    print(f"\nEvery session is in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
