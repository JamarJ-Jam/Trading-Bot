"""Demo-only Rise/Fall martingale, the same rules as Deriv's "Automate
Rise/Fall" screen, run by the bot so it can be watched from the app.

This exists to *measure* martingale on a demo account, not to make money:
our replays show it loses about 2.5% of everything staked on average. So it
refuses to run on anything but a demo account, checked twice: the config is
forced to demo and the account Deriv hands back must say demo.

Rules (see bot/martingale.py for the offline replay of the same rules):
  * Buy a Rise (CALL) or Fall (PUT) contract lasting `duration` ticks,
    seconds, minutes, hours or days, or ending at `end_time` (UTC, the next
    time the clock shows it), like the options on Deriv's trade screen.
  * After a loss the stake is multiplied; after a win it goes back to the
    start stake; a stake above `max_stake` goes back to the start stake.
  * A session ends at `profit` or `-loss`; the next one starts straight away
    until `sessions` sessions are done (0 = keep going).
  * A trade that could take the day below `-daily_loss_limit` is not placed;
    the runner waits for the next UTC day. The kill switch file stops it.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .deriv_client import DerivClient, DerivError, NotConnected
from .trade_log import TradeLog

log = logging.getLogger(__name__)

FATAL_CODES = {"401", "403", "InvalidToken", "AuthorizationRequired", "PermissionDenied"}
SESSION_FIELDS = ["start_utc", "end_utc", "trades", "result", "ended", "biggest_stake"]
# Deriv's Rise/Fall duration units: API letter and allowed range.
DURATION_UNITS = {"ticks": ("t", 1, 10), "seconds": ("s", 15, 86400), "minutes": ("m", 1, 1440),
                  "hours": ("h", 1, 24), "days": ("d", 1, 365)}
END_TIME = "end_time"


class MartingaleError(ValueError):
    pass


@dataclass
class MartingaleSettings:
    symbol: str = "1HZ100V"
    direction: str = "rise"          # rise or fall
    duration: int = 1
    duration_unit: str = "minutes"   # ticks, seconds, minutes, hours, days or end_time
    end_time: str = "23:59"          # HH:MM UTC, used when duration_unit is end_time
    stake: float = 1.0
    multiplier: float = 2.0
    max_stake: float = 64.0
    profit: float = 10.0             # session profit threshold
    loss: float = 100.0              # session loss threshold
    daily_loss_limit: float = 500.0
    sessions: int = 0                # 0 = keep going

    @classmethod
    def from_dict(cls, data: dict) -> MartingaleSettings:
        data = dict(data)
        if "duration_minutes" in data:  # settings saved before other units existed
            data.setdefault("duration", data["duration_minutes"])
            data.setdefault("duration_unit", "minutes")
            del data["duration_minutes"]
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise MartingaleError(f"unknown settings: {', '.join(sorted(unknown))}")
        merged = {**asdict(cls()), **data}
        try:
            s = cls(
                symbol=str(merged["symbol"]).strip(),
                direction=str(merged["direction"]).lower(),
                duration=_whole(merged["duration"], "duration"),
                duration_unit=str(merged["duration_unit"]).lower(),
                end_time=str(merged["end_time"]).strip(),
                stake=float(merged["stake"]), multiplier=float(merged["multiplier"]),
                max_stake=float(merged["max_stake"]), profit=float(merged["profit"]),
                loss=float(merged["loss"]), daily_loss_limit=float(merged["daily_loss_limit"]),
                sessions=_whole(merged["sessions"], "sessions"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, MartingaleError):
                raise
            raise MartingaleError("settings must be numbers") from None
        s.validate()
        return s

    def validate(self) -> None:
        if not self.symbol.replace("_", "").isalnum() or len(self.symbol) > 20:
            raise MartingaleError("symbol is not a market symbol")
        if self.direction not in ("rise", "fall"):
            raise MartingaleError("direction must be rise or fall")
        if self.duration_unit == END_TIME:
            _clock(self.end_time)
        elif self.duration_unit in DURATION_UNITS:
            _, low, high = DURATION_UNITS[self.duration_unit]
            if not low <= self.duration <= high:
                raise MartingaleError(f"duration must be {low} to {high} {self.duration_unit}")
        else:
            raise MartingaleError("duration unit must be ticks, seconds, minutes, hours, days "
                                  "or end time")
        if not 0.35 <= self.stake <= 1000:
            raise MartingaleError("stake must be 0.35 to 1000")
        if not 1 <= self.multiplier <= 10:
            raise MartingaleError("multiplier must be 1 to 10")
        if not self.stake <= self.max_stake <= 10000:
            raise MartingaleError("max stake must be at least the stake (and at most 10000)")
        if self.profit <= 0 or self.loss <= 0:
            raise MartingaleError("profit and loss thresholds must be above 0")
        if self.daily_loss_limit < self.max_stake:
            raise MartingaleError("daily loss limit must be at least the max stake")
        if not 0 <= self.sessions <= 100000:
            raise MartingaleError("sessions must be 0 (keep going) or a positive number")


def _whole(value, name: str) -> int:
    if isinstance(value, bool) or float(value) != int(float(value)):
        raise MartingaleError(f"{name} must be a whole number")
    return int(float(value))


def _clock(text: str) -> tuple[int, int]:
    try:
        hours, minutes = (int(part) for part in text.split(":"))
    except ValueError:
        raise MartingaleError("end time must look like 14:30 (UTC)") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise MartingaleError("end time must look like 14:30 (UTC)")
    return hours, minutes


def expiry_epoch(end_time: str, now: float, min_seconds: int = 60) -> int:
    """The next moment the UTC clock shows `end_time`, at least `min_seconds` away."""
    hours, minutes = _clock(end_time)
    day = int(now) - int(now) % 86400
    expiry = day + hours * 3600 + minutes * 60
    while expiry < now + min_seconds:
        expiry += 86400
    return expiry


def contract_length(s: MartingaleSettings, now: float) -> tuple[dict, float]:
    """Proposal fields for the contract length, and roughly how many seconds it runs."""
    if s.duration_unit == END_TIME:
        expiry = expiry_epoch(s.end_time, now)
        return {"date_expiry": expiry}, expiry - now
    letter = DURATION_UNITS[s.duration_unit][0]
    seconds = {"t": 2, "s": 1, "m": 60, "h": 3600, "d": 86400}[letter] * s.duration
    return {"duration": s.duration, "duration_unit": letter}, seconds


def next_stake(s: MartingaleSettings, stake: float, won: bool) -> float:
    if won:
        return s.stake
    bigger = round(stake * s.multiplier, 2)
    return s.stake if bigger > s.max_stake else bigger


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class MartingaleRunner:
    def __init__(self, cfg: Config, settings: MartingaleSettings, state_path: Path,
                 client_factory=DerivClient):
        # Demo, whatever the app's account setting says.
        self.cfg = replace(cfg, account_type="demo", dry_run=False)
        self.settings = settings
        self.state_path = Path(state_path)
        self.client = client_factory(self.cfg, self._on_message)
        self.trades = TradeLog(self.cfg.log_dir)
        self.sessions_path = Path(self.cfg.log_dir) / "martingale_sessions_live.csv"
        self.currency = "USD"
        self.status = "starting"
        self._stop = asyncio.Event()
        self._waiting: dict[str, asyncio.Future] = {}
        self.state = self._load()

    # ---- saved state ---------------------------------------------------------

    def _fresh(self) -> dict:
        return {"day": _today(), "day_pnl": 0.0, "session": None, "pending": None,
                "sessions_done": 0, "totals": {"sessions": 0, "up": 0, "down": 0, "result": 0.0,
                                               "trades": 0},
                "recent": []}

    def _load(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return self._fresh()
        return {**self._fresh(), **state}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    # ---- lifecycle -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.client.connect()
                account = self.client.account or {}
                if account.get("account_type") != "demo":
                    raise DerivError("Safety stop: the martingale test only runs on a demo account",
                                     "NotDemo")
                self.currency = account.get("currency") or "USD"
                backoff = 1.0
                await self._run_connected()
            except DerivError as exc:
                if exc.code in FATAL_CODES or exc.code == "NotDemo":
                    self.status = f"stopped: {exc}"
                    raise
                log.warning("Martingale: Deriv error: %s", exc)
                self.status = f"Deriv error, retrying: {exc}"
            except (OSError, NotConnected, asyncio.TimeoutError) as exc:
                log.warning("Martingale: connection problem: %s", exc or type(exc).__name__)
                self.status = "connection lost, reconnecting"
            finally:
                await self.client.close()
            if not self._stop.is_set():
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        if not self.status.startswith("stopped"):
            self.status = "stopped"

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_connected(self) -> None:
        reader = asyncio.create_task(self.client.reader(), name="martingale-reader")
        work = asyncio.create_task(self._trade_loop(), name="martingale-loop")
        stopper = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait({reader, work, stopper},
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is not stopper and task.exception():
                raise task.exception()
        if reader in done and not self._stop.is_set():
            raise NotConnected("connection closed by server")

    # ---- trading ---------------------------------------------------------------

    def _kill_switch(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    async def _trade_loop(self) -> None:
        s = self.settings
        while not self._stop.is_set():
            if self._kill_switch():
                self.status = "stopped: kill switch"
                self.stop()
                return
            if self.state["pending"]:
                await self._settle(self.state["pending"])
                continue
            if self.state["day"] != _today():
                self.state["day"], self.state["day_pnl"] = _today(), 0.0
                self._save()
            if s.sessions and self.state["sessions_done"] >= s.sessions:
                self.status = f"stopped: finished {s.sessions} session(s)"
                self.stop()
                return
            session = self.state["session"]
            if session is None:
                session = self.state["session"] = {"start": time.time(), "trades": 0,
                                                    "result": 0.0, "stake": s.stake,
                                                    "biggest": 0.0}
                self._save()
            stake = session["stake"]
            if stake > s.max_stake:  # settings changed since this session started
                stake = session["stake"] = s.stake
            if self.state["day_pnl"] - stake < -s.daily_loss_limit:
                self.status = (f"paused: a ${stake:g} trade could pass today's "
                               f"${s.daily_loss_limit:g} loss limit; waiting for tomorrow (UTC)")
                await self._sleep(60)
                continue
            await asyncio.shield(self._buy(stake))  # a stop never cuts a buy in half

    async def _buy(self, stake: float) -> None:
        s = self.settings
        kind = "CALL" if s.direction == "rise" else "PUT"
        quote = (await self.client.request({
            "proposal": 1, "amount": stake, "basis": "stake", "contract_type": kind,
            "currency": self.currency, "underlying_symbol": s.symbol,
            **contract_length(s, time.time())[0],
        })).get("proposal") or {}
        ask = float(quote.get("ask_price") or 0)
        if not quote.get("id") or ask <= 0 or ask > s.max_stake + 0.01:
            raise DerivError(f"unusable proposal (ask {ask:.2f}, max stake {s.max_stake:.2f})")
        bought = (await self.client.request({"buy": quote["id"], "price": ask})).get("buy") or {}
        cid = str(bought.get("contract_id", ""))
        if not cid:
            raise DerivError("buy response had no contract_id")
        price = float(bought.get("buy_price") or ask)
        self.state["pending"] = {"contract_id": cid, "stake": price}
        self._save()
        self.trades.write("opened", account_type="demo", symbol=s.symbol,
                          direction=s.direction.upper(), stake=f"{price:.2f}", contract_id=cid,
                          buy_price=f"{price:.2f}", reason="martingale test")
        self.status = f"in a ${price:g} {s.direction} trade"
        log.info("Martingale: bought %s %s for %.2f", s.direction, cid, price)

    async def _settle(self, pending: dict) -> None:
        cid = pending["contract_id"]
        fut = asyncio.get_running_loop().create_future()
        self._waiting[cid] = fut
        try:
            first = await self.client.request({"proposal_open_contract": 1,
                                               "contract_id": _api_id(cid), "subscribe": 1})
            await self._on_message(first)
            # No update by then: ask again (and keep asking every 10 minutes).
            limit = min(contract_length(self.settings, time.time())[1], 600) + 120
            while True:
                try:
                    poc = await asyncio.wait_for(asyncio.shield(fut), limit)
                    break
                except asyncio.TimeoutError:  # no update: ask once more
                    msg = await self.client.request({"proposal_open_contract": 1,
                                                     "contract_id": _api_id(cid)})
                    await self._on_message(msg)
                    if fut.done():
                        poc = fut.result()
                        break
        finally:
            self._waiting.pop(cid, None)
        self._record(pending, float(poc.get("profit") or 0), poc)

    async def _on_message(self, msg: dict) -> None:
        poc = msg.get("proposal_open_contract") or {}
        cid = str(poc.get("contract_id", ""))
        fut = self._waiting.get(cid)
        if fut is not None and poc.get("is_sold") and not fut.done():
            fut.set_result(poc)

    def _record(self, pending: dict, profit: float, poc: dict) -> None:
        s, st = self.settings, self.state
        won = profit > 0
        session = st["session"] or {"start": time.time(), "trades": 0, "result": 0.0,
                                    "stake": s.stake, "biggest": 0.0}
        session["trades"] += 1
        session["result"] = round(session["result"] + profit, 2)
        session["biggest"] = max(session["biggest"], pending["stake"])
        st["day_pnl"] = round(st["day_pnl"] + profit, 2)
        st["totals"]["trades"] += 1
        st["pending"] = None
        self.trades.write("closed", account_type="demo", symbol=s.symbol,
                          direction=s.direction.upper(), stake=f"{pending['stake']:.2f}",
                          contract_id=pending["contract_id"], buy_price=poc.get("buy_price", ""),
                          sell_price=poc.get("sell_price", ""), profit=f"{profit:.2f}",
                          reason="martingale test")
        ended = ("profit target" if session["result"] >= s.profit
                 else "loss limit" if session["result"] <= -s.loss else "")
        if ended:
            self._end_session(session, ended)
        else:
            session["stake"] = next_stake(s, pending["stake"], won)
            st["session"] = session
            self.status = (f"session {session['result']:+.2f} after {session['trades']} "
                           f"trade(s); next stake ${session['stake']:g}")
        self._save()
        log.info("Martingale: %s %+.2f (session %+.2f, today %+.2f)", pending["contract_id"],
                 profit, session["result"], st["day_pnl"])

    def _end_session(self, session: dict, ended: str) -> None:
        st = self.state
        now = time.time()
        row = {"start_utc": _iso(session["start"]), "end_utc": _iso(now),
               "trades": session["trades"], "result": session["result"], "ended": ended,
               "biggest_stake": session["biggest"]}
        new = not self.sessions_path.exists()
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.sessions_path.open("a", newline="") as f:
            w = csv.DictWriter(f, SESSION_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        totals = st["totals"]
        totals["sessions"] += 1
        totals["up" if ended == "profit target" else "down"] += 1
        totals["result"] = round(totals["result"] + session["result"], 2)
        st["recent"] = ([row] + st["recent"])[:30]
        st["sessions_done"] += 1
        st["session"] = None
        self.status = f"session ended {session['result']:+.2f} ({ended}); starting the next"

    def summary(self) -> dict:
        st = self.state
        return {"status": self.status, "today": st["day_pnl"], "session": st["session"],
                "pending": st["pending"], "totals": st["totals"], "recent": st["recent"]}


def _api_id(cid: str):
    return int(cid) if cid.isdigit() else cid
