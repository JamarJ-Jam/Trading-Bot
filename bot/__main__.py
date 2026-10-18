"""Run the bot:  python -m bot          (trade loop)
                python -m bot --check  (test the connection, then exit)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from .bot import TradingBot
from .config import Config, ConfigError
from .deriv_client import DerivClient, DerivError
from .trade_log import setup_logging

log = logging.getLogger("bot")


async def check(cfg: Config) -> int:
    """List accounts and open a connection, without trading."""

    async def ignore(_msg: dict) -> None:
        return None

    client = DerivClient(cfg, ignore)
    accounts = await client.get_accounts()
    log.info("Your Deriv accounts:")
    for acc in accounts:
        log.info("  %s  %-4s  %s %s  (%s)", acc.get("account_id"), acc.get("account_type"),
                 acc.get("currency"), acc.get("balance"), acc.get("status"))
    await client.connect()
    reader = asyncio.create_task(client.reader())
    try:
        tick = await client.request({"ticks": cfg.symbol})
        log.info("Latest %s price: %s", cfg.symbol, (tick.get("tick") or {}).get("quote"))
    finally:
        await client.close()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    log.info("Connection check passed. Nothing was traded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="test the token and connection, then exit without trading")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir, secrets=(cfg.api_token,))
    mode = "REAL MONEY" if cfg.is_real else "demo"
    log.info("Starting on %s account | strategy %s | stake %.2f | max daily loss %.2f",
             mode, cfg.strategy, cfg.stake, cfg.max_daily_loss)
    if cfg.is_real:
        log.warning("REAL MONEY MODE. Create the file %s to stop and close trades.",
                    cfg.kill_switch_file)

    try:
        if args.check:
            return asyncio.run(check(cfg))
        asyncio.run(TradingBot(cfg).run())
    except DerivError as exc:
        log.error("Stopped: %s", exc)
        return 1
    except ValueError as exc:
        log.error("Stopped: %s", exc)
        return 2
    except OSError as exc:
        log.error("Could not reach Deriv: %s. Check your internet connection.", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Stopped with Ctrl+C. Any open trades stay open on Deriv; "
                 "use the %s file next time to close them on the way out.",
                 cfg.kill_switch_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
