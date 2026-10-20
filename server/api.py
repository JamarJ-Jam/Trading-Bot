"""The app's web server: a JSON API under /api and the phone app itself.

Every /api route except login needs a valid session cookie, and every
request that changes something also needs the `X-Requested-With: app`
header, which a page on another site cannot send (cross-site request
forgery protection on top of the SameSite=Strict cookie).
"""

from __future__ import annotations

import csv
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.config import Config, ConfigError
from bot.risefall import MartingaleError
from bot.strategies.rules import CANDLE_SIZES, EXIT_PARAMS, RULES, SpecError

from . import lab
from .ai import AIError, IdeaGenerator
from .auth import Auth
from .martingale import MartingaleManager
from .pipeline import QUEUE_SIZE, Alerts, LabOptions, LabPipeline
from .runner import BotManager
from .settings import LAB_PREFIX, Settings

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE = "session"
DEFAULT_LAB_SYMBOLS = "frxEURUSD,frxGBPUSD,frxUSDJPY,frxAUDUSD"


class App:
    """Everything the routes need, built once from the environment."""

    def __init__(self, env: dict | None = None, *, fetch=None, ai_client=None, bot_factory=None,
                 martingale_factory=None):
        env = dict(os.environ if env is None else env)
        state = Path(env.get("STATE_DIR") or "state")
        self.env = env
        self.lab = lab.LabStore(state / "lab.json")
        self.settings = Settings(state / "app_settings.json", env, self.lab)
        self.alerts = Alerts(state / "alerts.json")
        kwargs = {"bot_factory": bot_factory} if bot_factory else {}
        self.bot = BotManager(self.settings, state, **kwargs)
        self.martingale = MartingaleManager(
            self.settings, state,
            **({"runner_factory": martingale_factory} if martingale_factory else {}))
        self.auth = Auth(env.get("APP_PASSWORD_HASH", ""), env.get("APP_TOTP_SECRET", ""),
                         env.get("APP_SECRET_KEY", ""))
        self.ai = IdeaGenerator(env.get("OPENROUTER_API_KEY", ""),
                                env.get("OPENROUTER_MODEL", "openrouter/auto"),
                                int(env.get("AI_MAX_IDEAS_PER_DAY") or 20), client=ai_client,
                                usage_path=state / "ai_usage.json")
        self.lab_symbols = [s.strip() for s in (env.get("LAB_SYMBOLS") or DEFAULT_LAB_SYMBOLS)
                            .split(",") if s.strip()]
        self.lab_options = LabOptions(state / "lab_options.json", self.ai.daily_cap)
        self.ai.daily_cap = self.lab_options.get()["ai_ideas_per_day"]
        self.pipeline = LabPipeline(self.lab, self.settings, self.alerts,
                                    **({"fetch": fetch} if fetch else {}), ai=self.ai,
                                    symbols=self.lab_symbols, options=self.lab_options)
        self.secure_cookie = (env.get("APP_SECURE_COOKIE", "true").lower() != "false")

    def config(self) -> Config:
        try:
            return self.settings.config()
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None


def create_app(env: dict | None = None, *, background: bool = True, **kwargs) -> FastAPI:
    state = App(env, **kwargs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            state.pipeline.start()
            await state.bot.resume_after_boot()
            if not state.bot.running:
                await state.martingale.resume_after_boot()
        yield
        await state.bot.shutdown()
        await state.martingale.shutdown()
        await state.pipeline.stop()

    app = FastAPI(title="Trading Bot", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.app = state

    def user(request: Request) -> None:
        if not state.auth.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "please log in")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "app":
            raise HTTPException(403, "missing app header")

    @app.exception_handler(ConfigError)
    async def config_error(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- session -----------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request):
        return {"logged_in": state.auth.valid(request.cookies.get(COOKIE)),
                "configured": state.auth.configured}

    @app.post("/api/login")
    async def login(request: Request, response: Response):
        if request.headers.get("x-requested-with") != "app":
            raise HTTPException(403, "missing app header")
        body = await request.json()
        wait = state.auth.locked_for()
        if wait:
            raise HTTPException(429, f"too many tries; wait {wait // 60 + 1} minutes")
        token = state.auth.login(str(body.get("password", "")), str(body.get("code", "")))
        if not token:
            if not state.auth.configured:
                raise HTTPException(503, "login is not set up on the server yet")
            raise HTTPException(401, "wrong password or code")
        response.set_cookie(COOKIE, token, max_age=7 * 86400, httponly=True,
                            secure=state.secure_cookie, samesite="strict", path="/")
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(user)])
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ---- bot -----------------------------------------------------------------

    @app.get("/api/status", dependencies=[Depends(user)])
    def status():
        cfg = state.config()
        risk = _risk_state(cfg)
        return {
            "bot": state.bot.status(),
            "settings": state.settings.public(cfg),
            "today": {
                "pnl": risk.get("realised_pnl", 0.0),
                "trades": risk.get("trades_opened", 0),
                "wins": risk.get("wins", 0),
                "losses": risk.get("losses", 0),
                "paused_until": risk.get("paused_until"),
                "open_exposure": round(sum((risk.get("open_stakes") or {}).values()), 2),
            },
            "kill_switch": Path(cfg.kill_switch_file).exists(),
            "recent": _trades(cfg, 5),
            "alerts": sum(1 for a in state.alerts.all() if not a["read"]),
        }

    @app.post("/api/bot/start", dependencies=[Depends(user)])
    async def start():
        if state.martingale.running:
            raise HTTPException(400, "stop the martingale test first; both would trade "
                                "the same demo account")
        try:
            await state.bot.start()
        except (ConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return state.bot.status()

    @app.post("/api/bot/pause", dependencies=[Depends(user)])
    async def pause():
        await state.bot.pause()
        return state.bot.status()

    @app.post("/api/bot/stop-all", dependencies=[Depends(user)])
    async def stop_all():
        await state.bot.stop_all()
        await state.martingale.stop()
        return state.bot.status()

    # ---- demo martingale test ----------------------------------------------------

    @app.get("/api/martingale", dependencies=[Depends(user)])
    def martingale_status():
        return state.martingale.status()

    @app.put("/api/martingale/settings", dependencies=[Depends(user)])
    async def martingale_settings(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "send an object of settings")
        try:
            state.martingale.update(body)
        except MartingaleError as exc:
            raise HTTPException(400, str(exc)) from None
        return state.martingale.status()

    @app.post("/api/martingale/start", dependencies=[Depends(user)])
    async def martingale_start():
        if state.bot.running:
            raise HTTPException(400, "pause the bot first; both would trade the same demo account")
        try:
            await state.martingale.start()
        except (ConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return state.martingale.status()

    @app.post("/api/martingale/stop", dependencies=[Depends(user)])
    async def martingale_stop():
        await state.martingale.stop()
        return state.martingale.status()

    @app.post("/api/martingale/reset", dependencies=[Depends(user)])
    def martingale_reset():
        try:
            state.martingale.reset()
        except MartingaleError as exc:
            raise HTTPException(400, str(exc)) from None
        return state.martingale.status()

    @app.get("/api/settings", dependencies=[Depends(user)])
    def get_settings():
        return state.settings.public(state.config())

    @app.put("/api/settings", dependencies=[Depends(user)])
    async def put_settings(request: Request):
        changes = await request.json()
        if not isinstance(changes, dict):
            raise HTTPException(400, "send an object of settings")
        cfg = state.settings.update(changes)
        await state.bot.restart_if_running()
        return state.settings.public(cfg)

    @app.post("/api/account", dependencies=[Depends(user)])
    async def account(request: Request):
        body = await request.json()
        cfg = state.settings.set_account(str(body.get("account_type", "")),
                                         str(body.get("confirm", "")),
                                         bool(body.get("acknowledged")))
        await state.bot.restart_if_running()
        return state.settings.public(cfg)

    @app.get("/api/trades", dependencies=[Depends(user)])
    def trades(limit: int = 200):
        return _trades(state.config(), max(1, min(limit, 1000)))

    # ---- lab -------------------------------------------------------------------

    @app.get("/api/lab", dependencies=[Depends(user)])
    def lab_list():
        return {"entries": state.lab.all(), "testing": state.pipeline.current,
                "required_t": round(lab.required_t(state.lab.tested_count() + 1), 2),
                "symbols": state.lab_symbols,
                "ai": {"configured": state.ai.configured, "remaining": state.ai.remaining_today()},
                "options": state.lab_options.get(), "auto_status": state.pipeline.auto_status,
                "queue_size": QUEUE_SIZE}

    @app.put("/api/lab/settings", dependencies=[Depends(user)])
    async def lab_settings_update(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "send an object")
        try:
            options = state.lab_options.update(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        state.ai.daily_cap = options["ai_ideas_per_day"]
        state.pipeline.poke()
        return options

    @app.post("/api/lab/delete-failed", dependencies=[Depends(user)])
    def lab_delete_failed():
        keep = state.pipeline._in_use()
        gone = state.lab.delete_where(lambda e: e["stage"] == "failed" and e["id"] != keep)
        return {"deleted": gone}

    @app.get("/api/lab/options", dependencies=[Depends(user)])
    def lab_options():
        return {"rules": {k: {"role": v["role"], "label": v["label"],
                              "params": {p: {"kind": kind, "min": lo, "max": hi, "default": d}
                                         for p, (kind, lo, hi, d) in v["params"].items()}}
                          for k, v in RULES.items()},
                "candle_sizes": CANDLE_SIZES, "exit": list(EXIT_PARAMS),
                "symbols": state.lab_symbols, "builtins": lab.BUILTINS}

    @app.post("/api/lab", dependencies=[Depends(user)])
    async def lab_add(request: Request):
        body = await request.json()
        try:
            entry = lab.new_entry(
                str(body.get("name", "")), "builder", list(body.get("symbols") or []),
                spec=body.get("spec") if body.get("spec") else None,
                builtin=body.get("builtin"),
                multiplier=body.get("multiplier"),
                commission=float(body.get("commission", lab.DEFAULT_COMMISSION)))
        except (lab.LabError, SpecError, TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        state.lab.add(entry)
        state.pipeline.poke()
        return entry

    @app.get("/api/lab/{entry_id}", dependencies=[Depends(user)])
    def lab_get(entry_id: str):
        entry = state.lab.get(entry_id)
        if not entry:
            raise HTTPException(404, "no such strategy")
        return entry

    @app.delete("/api/lab/{entry_id}", dependencies=[Depends(user)])
    def lab_delete(entry_id: str):
        cfg = state.config()
        if cfg.strategy == LAB_PREFIX + entry_id:
            raise HTTPException(400, "the bot is set to this strategy; pick another one first")
        if not state.lab.delete(entry_id):
            raise HTTPException(404, "no such strategy")
        return {"ok": True}

    @app.post("/api/lab/{entry_id}/use", dependencies=[Depends(user)])
    async def lab_use(entry_id: str):
        """Point the bot at a lab strategy (on whichever account is set).
        Works for any stage; the app warns when it is not ready."""
        entry = state.lab.get(entry_id)
        if not entry:
            raise HTTPException(404, "no such strategy")
        changes = {"strategy": LAB_PREFIX + entry_id, "symbol": entry["symbols"][0]}
        if entry.get("multiplier"):
            changes["multiplier"] = entry["multiplier"]
        cfg = state.settings.update(changes)
        await state.bot.restart_if_running()
        return state.settings.public(cfg)

    @app.post("/api/ai/ideas", dependencies=[Depends(user)])
    async def ai_ideas(request: Request):
        body = await request.json()
        state.config()
        try:
            added, rejected = await state.pipeline.add_ai_ideas(int(body.get("count") or 3))
        except AIError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"added": added, "rejected": rejected}

    @app.get("/api/alerts", dependencies=[Depends(user)])
    def alerts():
        return state.alerts.all()

    @app.post("/api/alerts/read", dependencies=[Depends(user)])
    def alerts_read():
        state.alerts.mark_read()
        return {"ok": True}

    # ---- the app itself ----------------------------------------------------------

    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

        @app.get("/sw.js")
        def service_worker():
            return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript")

        @app.get("/manifest.webmanifest")
        def manifest():
            return FileResponse(WEB_DIR / "manifest.webmanifest",
                                media_type="application/manifest+json")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app


def _risk_state(cfg: Config) -> dict:
    try:
        data = json.loads((Path(cfg.state_dir) / "risk_state.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}
    from bot.risk import utc_now
    if data.get("day") != utc_now().strftime("%Y-%m-%d"):
        return {"open_stakes": data.get("open_stakes") or {}}
    return data


def _trades(cfg: Config, limit: int) -> list[dict]:
    path = Path(cfg.log_dir) / "trades.csv"
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return list(reversed(rows[-limit:]))
