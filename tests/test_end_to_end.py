"""The real client and bot loop against a local fake Deriv WebSocket server,
including a dropped connection and the kill switch. No internet needed."""

import asyncio
import json
from dataclasses import replace

import websockets

from bot.bot import TradingBot
from bot.deriv_client import DerivClient
from bot.strategies import Signal, Strategy


class OnceStrategy(Strategy):
    name = "once"

    def __init__(self, ready):
        self.ready = ready
        self.fired = False

    def on_tick(self, price, epoch):
        if self.ready() and not self.fired:
            self.fired = True
            return Signal("DOWN", "e2e")
        return None


async def test_bot_against_fake_server(cfg, monkeypatch):
    monkeypatch.setattr("bot.bot.GUARD_INTERVAL", 0.05)
    cfg = replace(cfg, max_trade_minutes=0)
    connections = []
    sold = asyncio.Event()

    async def server(ws):
        connections.append(ws)
        poc_req = None
        async for raw in ws:
            msg = json.loads(raw)
            rid = msg["req_id"]
            if "portfolio" in msg:
                await ws.send(json.dumps({"req_id": rid, "msg_type": "portfolio",
                                          "portfolio": {"contracts": []}}))
            elif "ticks" in msg:
                for i in range(3):
                    await ws.send(json.dumps({"req_id": rid, "msg_type": "tick",
                                              "tick": {"quote": 100 + i, "epoch": 10 + i}}))
                if len(connections) == 1:
                    await asyncio.sleep(0.2)
                    await ws.close()  # simulate a dropped connection
                    return
            elif "proposal" in msg and "proposal_open_contract" not in msg:
                await ws.send(json.dumps({"req_id": rid, "msg_type": "proposal",
                                          "proposal": {"id": "PX", "ask_price": 1.0}}))
            elif "buy" in msg:
                await ws.send(json.dumps({"req_id": rid, "msg_type": "buy",
                                          "buy": {"contract_id": 42, "buy_price": 1.0}}))
            elif "proposal_open_contract" in msg:
                poc_req = rid
                await ws.send(json.dumps({"req_id": rid, "msg_type": "proposal_open_contract",
                                          "proposal_open_contract": {"contract_id": 42,
                                                                     "is_sold": 0}}))
            elif "sell" in msg:
                await ws.send(json.dumps({"req_id": rid, "msg_type": "sell", "sell": {}}))
                await ws.send(json.dumps({"req_id": poc_req, "msg_type": "proposal_open_contract",
                                          "proposal_open_contract": {"contract_id": 42,
                                                                     "is_sold": 1,
                                                                     "profit": -0.3}}))
                sold.set()

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]

        async def pick_account(self):
            return {"account_id": "DOT1", "account_type": "demo", "currency": "USD",
                    "balance": 10000, "status": "active"}

        async def get_ws_url(self, account_id):
            return f"ws://127.0.0.1:{port}"

        monkeypatch.setattr(DerivClient, "pick_account", pick_account)
        monkeypatch.setattr(DerivClient, "get_ws_url", get_ws_url)

        strategy = OnceStrategy(lambda: len(connections) >= 2)
        bot = TradingBot(cfg, strategy=strategy)
        task = asyncio.create_task(bot.run())

        # wait for the reconnect and the trade
        for _ in range(200):
            if len(connections) >= 2 and "42" in bot.open_trades:
                break
            await asyncio.sleep(0.05)
        assert len(connections) >= 2, "bot did not reconnect"
        assert "42" in bot.open_trades

        cfg.kill_switch_file.write_text("stop")
        await asyncio.wait_for(task, 10)
        assert sold.is_set()
        assert bot.open_trades == {}
        assert bot.risk.state.realised_pnl == -0.3
