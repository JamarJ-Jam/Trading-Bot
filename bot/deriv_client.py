"""Talks to Deriv: REST for accounts and a one-time WebSocket URL, then one
WebSocket for prices and trades.

Flow (from developers.deriv.com):
  1. GET  https://api.derivws.com/trading/v1/options/accounts
  2. POST https://api.derivws.com/trading/v1/options/accounts/{id}/otp
     -> {"data": {"url": "wss://.../ws/demo?otp=..."}}
  3. Connect to that URL. Every request carries a req_id; responses echo it.

REST calls send `Authorization: Bearer <token>` and `Deriv-App-ID: <app id>`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable

import websockets

from .config import Config

log = logging.getLogger(__name__)

REST_BASE = "https://api.derivws.com"
ACCOUNTS_PATH = "/trading/v1/options/accounts"

MessageHandler = Callable[[dict], Awaitable[None]]


class DerivError(Exception):
    """Deriv answered with an error."""

    def __init__(self, message: str, code: str = "", details: Any = None):
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.details = details


class NotConnected(ConnectionError):
    pass


class Throttle:
    """Spaces requests out so we stay far below Deriv's rate limits."""

    def __init__(self, per_second: float = 2.0):
        self.gap = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap


class DerivClient:
    def __init__(self, cfg: Config, on_message: MessageHandler):
        self.cfg = cfg
        self.on_message = on_message
        self.account: dict | None = None
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._throttle = Throttle()

    # ---- REST ----------------------------------------------------------

    def _rest_sync(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(REST_BASE + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.cfg.api_token}")
        req.add_header("Deriv-App-ID", self.cfg.app_id)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            errors = payload.get("errors") or [{}]
            first = errors[0] if isinstance(errors[0], dict) else {}
            raise DerivError(
                first.get("message") or f"HTTP {exc.code} from {path}",
                code=str(first.get("code") or exc.code),
                details=payload,
            ) from None

    async def rest(self, method: str, path: str, body: dict | None = None) -> dict:
        return await asyncio.to_thread(self._rest_sync, method, path, body)

    async def get_accounts(self) -> list[dict]:
        return (await self.rest("GET", ACCOUNTS_PATH)).get("data") or []

    async def pick_account(self) -> dict:
        """Choose the account to trade on. Never picks real unless configured."""
        accounts = await self.get_accounts()
        wanted = self.cfg.account_type
        for acc in accounts:
            if self.cfg.account_id and acc.get("account_id") != self.cfg.account_id:
                continue
            if acc.get("account_type") != wanted:
                continue
            if acc.get("status", "active") != "active":
                continue
            return acc
        found = ", ".join(
            f"{a.get('account_id')} ({a.get('account_type')}, {a.get('status')})" for a in accounts
        ) or "none"
        raise DerivError(f"No active {wanted} account matched. Accounts found: {found}")

    async def get_ws_url(self, account_id: str) -> str:
        result = await self.rest("POST", f"{ACCOUNTS_PATH}/{account_id}/otp")
        url = (result.get("data") or {}).get("url")
        if not url:
            raise DerivError("OTP response did not include a WebSocket URL")
        return url

    # ---- WebSocket -----------------------------------------------------

    async def connect(self) -> None:
        """Pick the account, fetch a fresh one-time URL and open the socket."""
        self.account = await self.pick_account()
        if self.account.get("account_type") == "real" and not self.cfg.is_real:
            raise DerivError("Safety stop: picked a real account while configured for demo")
        url = await self.get_ws_url(self.account["account_id"])
        self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=20,
                                            max_size=2**22)
        log.info("Connected to %s account %s (%s %.2f)",
                 self.account.get("account_type"), self.account.get("account_id"),
                 self.account.get("currency"), float(self.account.get("balance") or 0))

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        self._ws = None

    async def reader(self) -> None:
        """Read messages until the socket closes. Raises when it drops."""
        if self._ws is None:
            raise NotConnected()
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("Ignoring non-JSON message")
                    continue
                fut = self._pending.pop(msg.get("req_id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                else:
                    await self.on_message(msg)
        finally:
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(NotConnected("connection closed"))
            self._pending.clear()

    async def request(self, payload: dict, timeout: float = 15.0) -> dict:
        """Send one request and wait for its matching response.

        For subscriptions, the first response comes back here and later
        updates go to `on_message`.
        """
        await self._throttle.wait()
        ws = self._ws
        if ws is None:
            raise NotConnected("not connected")
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send(json.dumps({**payload, "req_id": req_id}))
            msg = await asyncio.wait_for(fut, timeout)
        except websockets.ConnectionClosed as exc:
            raise NotConnected(f"connection closed: {exc}") from None
        finally:
            self._pending.pop(req_id, None)
        if "error" in msg:
            err = msg["error"] or {}
            raise DerivError(err.get("message", "unknown error"), err.get("code", ""), err)
        return msg
