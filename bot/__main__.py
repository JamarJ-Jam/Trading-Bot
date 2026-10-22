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
from .deriv_client import DerivClient, DerivError, NotConnected
from .mt5_client import Mt5Client
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


async def check_mt5(cfg: Config) -> int:
    """Attach to the MetaTrader 5 terminal and read one price, without trading."""

    async def ignore(_msg: dict) -> None:
        return None

    client = Mt5Client(cfg, ignore)
    await client.connect()
    try:
        acc = client.account or {}
        log.info("MT5 account %s (%s) on %s: %s %s", acc.get("account_id"),
                 acc.get("account_type"), acc.get("server"), acc.get("currency"),
                 acc.get("balance"))
        tick = (await client.request({"ticks": cfg.symbol})).get("tick") or {}
        log.info("Latest %s price: bid %s, ask %s", cfg.symbol, tick.get("quote"), tick.get("ask"))
        quote = (await client.request({
            "proposal": 1, "amount": cfg.stake, "contract_type": "MULTUP",
            "limit_order": {"stop_loss": cfg.stop_loss} if cfg.stop_loss else {},
        })).get("proposal") or {}
        log.info("A buy now would be %s lots with its stop loss at %s, risking %.2f",
                 quote.get("volume"), quote.get("stop_loss_price"), quote.get("ask_price"))
    finally:
        await client.close()
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

    setup_logging(cfg.log_dir, secrets=(cfg.api_token, cfg.mt5_password))
    mode = "REAL MONEY" if cfg.is_real else "demo"
    log.info("Starting on %s %s account | strategy %s | stake %.2f | max daily loss %.2f",
             "MetaTrader 5" if cfg.is_mt5 else "Deriv", mode, cfg.strategy, cfg.stake,
             cfg.max_daily_loss)
    if cfg.is_real:
        log.warning("REAL MONEY MODE. Create the file %s to stop and close trades.",
                    cfg.kill_switch_file)

    try:
        if args.check:
            return asyncio.run(check_mt5(cfg) if cfg.is_mt5 else check(cfg))
        asyncio.run(TradingBot(cfg, close_on_interrupt=True).run())
    except (DerivError, NotConnected) as exc:
        log.error("Stopped: %s", exc)
        return 1
    except ValueError as exc:
        log.error("Stopped: %s", exc)
        return 2
    except OSError as exc:
        log.error("Could not reach %s: %s. Check your internet connection.",
                  "MetaTrader 5" if cfg.is_mt5 else "Deriv", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Stopped with Ctrl+C. Any trade still open stays open on %s with its "
                 "own stop loss; the bot picks it up again on the next start.",
                 "MetaTrader 5" if cfg.is_mt5 else "Deriv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
