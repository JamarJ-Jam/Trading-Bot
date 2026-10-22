import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bot.backtest import Candle
from server import lab
from server.ai import IdeaGenerator, parse_ideas
from server.api import create_app
from server.auth import Auth, check_password, check_totp, hash_password, totp

SECRET = "JBSWY3DPEHPK3PXP"
HEADERS = {"X-Requested-With": "app"}


def env(tmp_path, **extra):
    base = {
        "DERIV_API_TOKEN": "tok", "DERIV_APP_ID": "app", "STATE_DIR": str(tmp_path / "state"),
        "LOG_DIR": str(tmp_path / "logs"), "KILL_SWITCH_FILE": str(tmp_path / "state" / "STOP"),
        "APP_PASSWORD_HASH": hash_password("correct horse battery"),
        "APP_TOTP_SECRET": SECRET, "APP_SECRET_KEY": "k" * 64, "APP_SECURE_COOKIE": "false",
        "MAX_DAILY_LOSS": "10", "STAKE": "1", "MAX_STAKE": "2",
    }
    base.update(extra)
    return base


class FakeBot:
    def __init__(self, cfg, strategy=None):
        self.cfg, self.strategy = cfg, strategy
        self.open_trades = {}
        self.client = type("C", (), {"account": {"balance": 10000, "currency": "USD"}})()
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        # Like the real guard loop: the STOP file ends the run.
        while not self._stop.is_set() and not self.cfg.kill_switch_file.exists():
            await asyncio.sleep(0.01)


def client(tmp_path, **kwargs):
    app = create_app(env(tmp_path, **kwargs.pop("extra", {})), background=False,
                     bot_factory=FakeBot, **kwargs)
    return TestClient(app)


def login(c):
    r = c.post("/api/login", json={"password": "correct horse battery", "code": totp(SECRET)},
               headers=HEADERS)
    assert r.status_code == 200, r.text


# ---- auth ----------------------------------------------------------------------

def test_password_hash_round_trip():
    stored = hash_password("s3cret-password")
    assert check_password("s3cret-password", stored)
    assert not check_password("wrong", stored)
    assert not check_password("x", "garbage")


def test_totp_known_vector_and_window():
    # RFC 6238 test key "12345678901234567890" in base32, at t=59 -> 287082
    key = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp(key, 59) == "287082"
    assert check_totp(key, "287082", 59 + 30)
    assert not check_totp(key, "287082", 59 + 120)
    assert not check_totp(key, "abc", 59)


def test_auth_locks_out_after_failures():
    now = [1000.0]
    auth = Auth(hash_password("pw-long-enough"), SECRET, "k" * 64, clock=lambda: now[0])
    for _ in range(5):
        assert auth.login("bad", "000000") is None
    assert auth.locked_for() > 0
    assert auth.login("pw-long-enough", totp(SECRET, now[0])) is None
    now[0] += 16 * 60
    token = auth.login("pw-long-enough", totp(SECRET, now[0]))
    assert token and auth.valid(token)
    assert not auth.valid(token[:-1] + ("0" if token[-1] != "0" else "1"))


def test_unconfigured_auth_refuses_everyone(tmp_path):
    c = client(tmp_path, extra={"APP_SECRET_KEY": ""})
    r = c.post("/api/login", json={"password": "correct horse battery", "code": totp(SECRET)},
               headers=HEADERS)
    assert r.status_code == 503


def test_api_needs_login_and_app_header(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/login", json={}).status_code == 403
    login(c)
    assert c.get("/api/status").status_code == 200
    assert c.post("/api/bot/start").status_code == 403  # no app header


# ---- bot controls ------------------------------------------------------------------

def test_start_pause_and_stop_all(tmp_path):
    with client(tmp_path) as c:
        login(c)
        stop_file = tmp_path / "state" / "STOP"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("x")
        r = c.post("/api/bot/start", headers=HEADERS)
        assert r.json()["state"] == "running"
        assert not stop_file.exists()
        status = c.get("/api/status").json()["bot"]
        assert status["price"] is None and status["price_age"] is None and status["note"] == ""
        assert c.post("/api/bot/pause", headers=HEADERS).json()["state"] == "stopped"
        c.post("/api/bot/start", headers=HEADERS)
        assert c.post("/api/bot/stop-all", headers=HEADERS).json()["state"] == "stopped"
        assert stop_file.exists()
        assert c.get("/api/status").json()["kill_switch"] is True


def test_settings_are_validated_like_the_bot(tmp_path):
    c = client(tmp_path)
    login(c)
    r = c.put("/api/settings", json={"stake": 5}, headers=HEADERS)
    assert r.status_code == 400 and "MAX_STAKE" in r.json()["detail"]
    r = c.put("/api/settings", json={"strategy": "trend", "stake": 1.5}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["stake"] == 1.5
    assert c.put("/api/settings", json={"account_type": "real"}, headers=HEADERS).status_code == 400
    assert c.put("/api/settings", json={"strategy": "nope"}, headers=HEADERS).status_code == 400


def test_mt5_app_opens_without_a_market_but_will_not_trade_until_one_is_picked(tmp_path):
    with client(tmp_path, extra={"BROKER": "mt5", "DERIV_API_TOKEN": ""}) as c:
        login(c)
        assert c.get("/api/status").json()["settings"]["symbol"] == ""
        r = c.post("/api/bot/start", headers=HEADERS)
        assert r.status_code == 400 and "pick a market" in r.json()["detail"]
        r = c.put("/api/settings", json={"symbol": "  "}, headers=HEADERS)
        assert r.status_code == 400 and "pick a market" in r.json()["detail"]
        assert c.put("/api/settings", json={"symbol": "EURUSD"}, headers=HEADERS).status_code == 200
        assert c.post("/api/bot/start", headers=HEADERS).json()["state"] == "running"


def test_markets_follow_the_broker_and_can_be_overridden(tmp_path):
    c = client(tmp_path)
    login(c)
    data = c.get("/api/markets").json()
    values = [i["value"] for g in data["markets"] for i in g["items"]]
    assert "cryETHUSD" in values and "1HZ100V" in values

    mt5 = client(tmp_path, extra={"BROKER": "mt5", "SYMBOL": "EURUSD", "DERIV_API_TOKEN": ""})
    login(mt5)
    data = mt5.get("/api/markets").json()
    assert "EURUSD" in [i["value"] for g in data["markets"] for i in g["items"]]
    assert "cryETHUSD" in [i["value"] for g in data["deriv"] for i in g["items"]]

    status = mt5.get("/api/status").json()
    assert status["settings"]["broker"] == "mt5" and "server" in status["bot"]
    assert c.get("/api/status").json()["settings"]["broker"] == "deriv"

    mine = client(tmp_path, extra={"MARKETS_DERIV": "frxEURUSD:Euro, cryETHUSD"})
    login(mine)
    (group,) = mine.get("/api/markets").json()["markets"]
    assert group["items"] == [{"value": "frxEURUSD", "label": "Euro"},
                              {"value": "cryETHUSD", "label": "cryETHUSD"}]


def test_real_account_needs_typed_confirmation(tmp_path):
    c = client(tmp_path)
    login(c)
    r = c.post("/api/account", json={"account_type": "real", "confirm": "yes",
                                     "acknowledged": True}, headers=HEADERS)
    assert r.status_code == 400
    r = c.post("/api/account", json={"account_type": "real", "confirm": "REAL",
                                     "acknowledged": True}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["account_type"] == "real"
    r = c.post("/api/account", json={"account_type": "demo"}, headers=HEADERS)
    assert r.json()["account_type"] == "demo"


# ---- lab ------------------------------------------------------------------------------

SPEC = {"candle_seconds": 3600, "entry": [{"type": "momentum", "lookback": 3}],
        "exit": {"exit_on_reverse": True}}


def waves(n=3000):
    """Smooth hourly waves: a momentum strategy that flips with them wins."""
    import math
    start = (int(time.time()) // 3600 - n) * 3600
    out = []
    for i in range(n):
        p = 1 + 0.01 * math.sin(i * 2 * math.pi / 48)
        out.append(Candle(start + i * 3600, p, p, p, p))
    return out


def test_builder_entry_runs_through_the_pipeline(tmp_path):
    async def fetch(symbol, gran, count):
        return waves()

    c = client(tmp_path, fetch=fetch)
    login(c)
    r = c.post("/api/lab", json={"name": "Momentum", "symbols": ["frxEURUSD"], "spec": SPEC,
                                 "multiplier": 100}, headers=HEADERS)
    assert r.status_code == 200, r.text
    entry_id = r.json()["id"]
    app_state = c.app.state.app
    asyncio.run(app_state.pipeline.step(now=0))
    entry = c.get(f"/api/lab/{entry_id}").json()
    assert entry["stage"] == "paper", entry["status"]
    assert [x["ok"] for x in entry["checks"]] == [True, True, False, True]

    # Fourteen days of paper trading later it is ready and raises an alert.
    entry["paper_since"] -= 15 * 86400
    app_state.lab.save(entry)
    asyncio.run(app_state.pipeline.paper(entry))
    entry = c.get(f"/api/lab/{entry_id}").json()
    assert entry["stage"] == "ready", entry["status"]
    assert c.get("/api/status").json()["alerts"] == 1

    r = c.post(f"/api/lab/{entry_id}/use", headers=HEADERS)
    assert r.json()["strategy_name"] == "Momentum"
    assert c.delete(f"/api/lab/{entry_id}", headers=HEADERS).status_code == 400


def test_backtest_cut_off_by_a_restart_runs_again(tmp_path):
    from server import lab
    from server.pipeline import LabPipeline

    store = lab.LabStore(tmp_path / "lab.json")
    entry = store.add(lab.new_entry("Cut off", "ai", ["frxEURUSD"], spec=SPEC))
    entry["stage"], entry["status"] = "backtest", "Downloading prices and backtesting"
    store.save(entry)

    async def boot():
        pipeline = LabPipeline(store, None, None)
        pipeline.start()
        await pipeline.stop()

    asyncio.run(boot())
    assert store.get(entry["id"])["stage"] == "queued"


def test_bad_builder_spec_is_rejected(tmp_path):
    c = client(tmp_path)
    login(c)
    bad = {"candle_seconds": 3600, "entry": [{"type": "magic"}], "exit": {"take_profit": 1}}
    r = c.post("/api/lab", json={"name": "x", "symbols": ["frxEURUSD"], "spec": bad},
               headers=HEADERS)
    assert r.status_code == 400


# ---- AI ideas ---------------------------------------------------------------------------

def test_parse_ideas_keeps_only_valid_new_specs():
    text = "Sure! " + json.dumps({"ideas": [
        {"name": "Good", "why": "trend", "symbols": ["frxEURUSD", "frxXXX"], "spec": SPEC},
        {"name": "Same again", "symbols": ["frxEURUSD"], "spec": SPEC},
        {"name": "Bad rule", "symbols": ["frxEURUSD"],
         "spec": {"candle_seconds": 3600, "entry": [{"type": "rm -rf"}], "exit": {}}},
        {"name": "Bad market", "symbols": ["frxXXX"], "spec": SPEC},
    ]})
    good, rejected = parse_ideas(text, ["frxEURUSD"], [])
    assert [g["name"] for g in good] == ["Good"]
    assert good[0]["symbols"] == ["frxEURUSD"]
    assert len(rejected) == 3


def test_ai_ideas_endpoint_adds_ideas_to_the_lab(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        content = json.dumps({"ideas": [{"name": "AI one", "why": "because",
                                         "symbols": ["frxEURUSD"], "spec": SPEC}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    ai_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = client(tmp_path, ai_client=ai_client, extra={"OPENROUTER_API_KEY": "or-key"})
    login(c)
    r = c.post("/api/ai/ideas", json={"count": 2}, headers=HEADERS)
    assert r.status_code == 200, r.text
    added = r.json()["added"]
    assert added[0]["source"] == "ai" and added[0]["note"] == "because"
    assert seen["auth"] == "Bearer or-key"
    assert "frxEURUSD" in seen["body"]["messages"][1]["content"]


def test_parse_ideas_forgives_formatting_slips():
    loose = {"candle_seconds": "900",
             "entry": [{"type": "breakout", "lookback": "8"}],
             "exit": {"stop_loss": "0.5", "take_profit": None, "exit_on_reverse": "false"}}
    text = json.dumps({"ideas": [
        {"name": "Strings", "symbols": ["EUR/USD"], "spec": loose},
        {"name": "Flat", "symbols": ["EURUSD"], "candle_seconds": 3600,
         "entry": [{"type": "momentum", "lookback": 4}], "exit": {"max_trade_minutes": 60}},
    ]})
    good, rejected = parse_ideas(text, ["frxEURUSD"], [])
    assert rejected == []
    assert good[0]["symbols"] == ["frxEURUSD"]
    assert good[0]["spec"]["candle_seconds"] == 900 and good[0]["spec"]["exit"]["stop_loss"] == 0.5
    assert good[1]["spec"]["entry"][0]["lookback"] == 4


def test_ai_asks_once_to_fix_rejected_ideas(tmp_path):
    bad = {"candle_seconds": 900, "entry": [{"type": "macd"}], "exit": {"max_trade_minutes": 60}}
    replies = [{"ideas": [{"name": "Bad", "symbols": ["frxEURUSD"], "spec": bad}]},
               {"ideas": [{"name": "Fixed", "symbols": ["frxEURUSD"], "spec": SPEC}]}]
    asked = []

    def handler(request: httpx.Request):
        asked.append(json.loads(request.content)["messages"])
        content = json.dumps(replies[len(asked) - 1])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    gen = IdeaGenerator("key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    good, rejected = asyncio.run(gen.ideas(1, ["frxEURUSD"], [], 10))
    assert [g["name"] for g in good] == ["Fixed"] and rejected == []
    assert len(asked) == 2 and "macd" in asked[1][-1]["content"]


def test_ai_prompt_hides_unseen_and_paper_results():
    from server.ai import build_prompt

    entry = {"name": "Old", "symbols": ["frxEURUSD"], "description": "d",
             "results": {"backtest": {"trades": 40, "net": 3.0, "win_rate": 0.5, "t_stat": 1.0},
                         "holdout": {"trades": 12, "net": -987.65},
                         "paper": {"trades": 3, "net": -555.44}}}
    prompt = build_prompt(2, ["frxEURUSD"], [entry], 10)[1]["content"]
    assert "+3.00" in prompt
    assert "987.65" not in prompt and "555.44" not in prompt


def test_ai_daily_cap():
    gen = IdeaGenerator("key", daily_cap=2, clock=lambda: 0)
    gen._asked = [0, 0]
    with pytest.raises(Exception, match="daily limit"):
        asyncio.run(gen.ideas(1, ["frxEURUSD"], [], 10))


def test_app_page_is_served(tmp_path):
    c = client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert "Content-Security-Policy" in r.headers


# ---- auto ideas and cleanup ------------------------------------------------------------

def ai_handler(calls):
    """Each call returns as many distinct valid ideas as were asked for."""
    def handler(request: httpx.Request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        count = int(prompt.split("Propose ")[1].split()[0])
        calls.append(count)
        ideas = [{"name": f"Idea {len(calls)}.{i}", "why": "w", "symbols": ["frxEURUSD"],
                  "spec": {**SPEC, "entry": [{"type": "momentum", "lookback": 10 * len(calls) + i}]}}
                 for i in range(count)]
        content = json.dumps({"ideas": ideas})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return handler


def test_auto_ideas_fill_the_queue_to_five_then_pause(tmp_path):
    calls = []
    ai_client = httpx.AsyncClient(transport=httpx.MockTransport(ai_handler(calls)))
    c = client(tmp_path, ai_client=ai_client, extra={"OPENROUTER_API_KEY": "or-key"})
    login(c)
    pipeline = c.app.state.app.pipeline
    asyncio.run(pipeline.top_up(time.time()))
    assert calls == [] and pipeline.auto_status == "Off"

    r = c.put("/api/lab/settings", json={"auto_ideas": True}, headers=HEADERS)
    assert r.json()["auto_ideas"] is True
    asyncio.run(pipeline.top_up(time.time()))
    assert calls == [5] and pipeline.waiting() == 5
    asyncio.run(pipeline.top_up(time.time()))
    assert calls == [5] and "Paused" in pipeline.auto_status

    # One idea leaves the queue: only one more is asked for.
    entry = c.get("/api/lab").json()["entries"][0]
    c.delete(f"/api/lab/{entry['id']}", headers=HEADERS)
    asyncio.run(pipeline.top_up(time.time()))
    assert calls == [5, 1]
    assert c.get("/api/lab").json()["auto_status"] == "Added 1 idea"

    assert c.put("/api/lab/settings", json={"delete_failed_days": 2},
                 headers=HEADERS).status_code == 400


def test_failed_ideas_are_deleted_but_still_count(tmp_path):
    c = client(tmp_path)
    login(c)
    app_state = c.app.state.app
    store, pipeline = app_state.lab, app_state.pipeline
    now = time.time()
    entries = []
    for i, (stage, age_days) in enumerate([("failed", 5), ("failed", 1), ("paper", 9)]):
        e = lab.new_entry(f"E{i}", "ai", ["frxEURUSD"],
                          spec={**SPEC, "entry": [{"type": "momentum", "lookback": i + 1}]})
        e["stage"], e["updated_at"] = stage, now - age_days * 86400
        e["results"] = {"backtest": {"trades": 40, "net": -1.0, "win_rate": 0.4, "t_stat": -0.5}}
        entries.append(store.add(e))
    assert store.tested_count() == 3

    assert pipeline.delete_old_failures(now) == 1  # default: failed for 3+ days
    assert [e["name"] for e in store.all()] == ["E2", "E1"]
    assert store.tested_count() == 3 and store.history()[0]["name"] == "E0"

    r = c.post("/api/lab/delete-failed", headers=HEADERS)
    assert r.json() == {"deleted": 1}
    assert [e["name"] for e in store.all()] == ["E2"] and store.tested_count() == 3

    # The AI still sees (and cannot re-propose) deleted ideas.
    good, rejected = parse_ideas(json.dumps({"ideas": [
        {"name": "Again", "symbols": ["frxEURUSD"], "spec": entries[0]["spec"]}]}),
        ["frxEURUSD"], store.all() + store.history())
    assert good == [] and "already tested" in rejected[0]


def test_ai_daily_cap_survives_a_restart(tmp_path):
    path = tmp_path / "ai_usage.json"
    calls = []
    gen = IdeaGenerator("key", daily_cap=3, usage_path=path,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(ai_handler(calls))))
    asyncio.run(gen.ideas(2, ["frxEURUSD"], [], 10))
    assert IdeaGenerator("key", daily_cap=3, usage_path=path).remaining_today() == 1


def test_ai_ideas_per_day_is_a_lab_setting(tmp_path):
    c = client(tmp_path, extra={"OPENROUTER_API_KEY": "or-key", "AI_MAX_IDEAS_PER_DAY": "20"})
    login(c)
    assert c.get("/api/lab").json()["options"]["ai_ideas_per_day"] == 20
    r = c.put("/api/lab/settings", json={"ai_ideas_per_day": 100}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["ai_ideas_per_day"] == 100
    assert c.get("/api/lab").json()["ai"]["remaining"] == 100
    for bad in (0, 501, "50", True):
        assert c.put("/api/lab/settings", json={"ai_ideas_per_day": bad},
                     headers=HEADERS).status_code == 400
    # A restart keeps the saved value.
    assert client(tmp_path, extra={"OPENROUTER_API_KEY": "or-key"}).app.state.app.ai.daily_cap == 100


class FakeMartingale:
    def __init__(self, cfg, settings, state_path):
        self.cfg, self.settings = cfg, settings
        self.state = {"sessions_done": 0}
        self.status = "starting"
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        self.status = "running"
        await self._stop.wait()
        self.status = "stopped"

    def summary(self):
        return {"status": self.status, "today": 0.0, "session": None, "pending": None,
                "totals": {"sessions": 0, "up": 0, "down": 0, "result": 0.0, "trades": 0},
                "recent": []}


def test_martingale_test_and_bot_never_run_together(tmp_path):
    with client(tmp_path, martingale_factory=FakeMartingale) as c:
        login(c)
        r = c.put("/api/martingale/settings", json={"stake": 2, "max_stake": 32}, headers=HEADERS)
        assert r.status_code == 200 and r.json()["settings"]["stake"] == 2
        assert c.put("/api/martingale/settings", json={"direction": "sideways"},
                     headers=HEADERS).status_code == 400

        assert c.post("/api/martingale/start", headers=HEADERS).json()["running"] is True
        assert c.post("/api/bot/start", headers=HEADERS).status_code == 400
        assert c.put("/api/martingale/settings", json={"stake": 3},
                     headers=HEADERS).status_code == 400  # not while running
        assert c.post("/api/martingale/stop", headers=HEADERS).json()["running"] is False

        assert c.post("/api/bot/start", headers=HEADERS).status_code == 200
        assert c.post("/api/martingale/start", headers=HEADERS).status_code == 400
        c.post("/api/bot/stop-all", headers=HEADERS)
