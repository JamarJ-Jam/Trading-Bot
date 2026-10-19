"""AI strategy ideas through OpenRouter.

The model only ever proposes strategies in the builder's rule format.
Every idea is checked by `validate_spec`, so it cannot invent rules,
and goes through the full lab pipeline like any other idea. It never
trades, never changes settings, and never sees the unseen-data or paper
results (only backtest numbers), so it cannot tune ideas to the held-back
prices.

Settings (in .env):
  OPENROUTER_API_KEY      use a separate key with a credit limit for this app
  OPENROUTER_MODEL        any OpenRouter model id (default openrouter/auto)
  AI_MAX_IDEAS_PER_DAY    hard cap on ideas the app will ask for (default 20)
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from bot.strategies.rules import CANDLE_SIZES, EXIT_PARAMS, RULES, SpecError, validate_spec

URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_PER_REQUEST = 5

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    pass


def _catalogue() -> str:
    lines = []
    for name, meta in RULES.items():
        params = ", ".join(f"{p} ({kind} {lo}-{hi}, default {d})"
                           for p, (kind, lo, hi, d) in meta["params"].items())
        lines.append(f"- {name} [{meta['role']}]: {meta['label']}. Params: {params}")
    return "\n".join(lines)


def _history(entries: list[dict], limit: int = 25) -> str:
    """Past ideas with backtest numbers only. Later-stage results are left
    out on purpose so the AI cannot fit ideas to the held-back data."""
    lines = []
    for e in entries[:limit]:
        bt = (e.get("results") or {}).get("backtest")
        if not bt:
            continue
        lines.append(f"- {e['name']} on {', '.join(e['symbols'])}: {e['description']}; "
                     f"backtest {bt['trades']} trades, net {bt['net']:+.2f}, "
                     f"win rate {bt['win_rate']:.0%}, score {bt['t_stat']}")
    return "\n".join(lines) or "- none yet"


def build_prompt(count: int, symbols: list[str], entries: list[dict], max_daily_loss: float) -> list[dict]:
    system = (
        "You design simple, testable trading strategies for a small personal bot that trades "
        "Deriv multiplier contracts with a fixed $1 stake. Costs are included in every test, and "
        "most simple ideas lose money after costs, so prefer few, robust rules over clever "
        "combinations. You only output JSON."
    )
    example = {
        "ideas": [{
            "name": "London breakout with trend filter",
            "why": "One or two sentences on the market behaviour it tries to capture.",
            "symbols": symbols[:2],
            "spec": {
                "candle_seconds": 900,
                "entry": [{"type": "breakout", "lookback": 8},
                          {"type": "price_vs_ema", "length": 100},
                          {"type": "session", "start_hour": 7, "end_hour": 12}],
                "exit": {"stop_loss": 0.5, "take_profit": 1.0, "max_trade_minutes": 480,
                         "exit_on_reverse": False},
            },
        }]
    }
    user = f"""Propose {count} new strategy ideas, different from the ones already tested.

Rules you can combine (direction rules must all agree to trade; filters only allow or block):
{_catalogue()}

candle_seconds must be one of {", ".join(map(str, CANDLE_SIZES))}.
exit fields: {", ".join(EXIT_PARAMS)} (stop_loss and take_profit in dollars on a $1 stake;
at least one exit is required). Daily loss limit is ${max_daily_loss:.2f}.
Markets to choose from: {", ".join(symbols)}.

Already tested (backtest results only):
{_history(entries)}

Reply with JSON only, exactly in this shape:
{json.dumps(example, indent=1)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _coerce(value):
    """Undo harmless formatting slips models make: numbers and booleans
    written as strings. The result still goes through validate_spec."""
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        if text.lower() in ("null", "none", ""):
            return None
        try:
            number = float(text)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def _market(name, allowed: list[str]) -> str | None:
    """Match "frxEURUSD", "EURUSD" or "EUR/USD" to an allowed market."""
    key = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    for symbol in allowed:
        plain = re.sub(r"[^A-Z0-9]", "", symbol.upper())
        if key in (plain, plain.removeprefix("FRX")):
            return symbol
    return None


def parse_ideas(text: str, allowed_symbols: list[str], existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (valid ideas, reasons for each rejected one)."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise AIError("the AI reply had no JSON in it")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise AIError(f"the AI reply was not valid JSON ({exc})") from None
    ideas = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(ideas, list):
        raise AIError("the AI reply had no ideas list")

    seen = [json.dumps(e.get("spec"), sort_keys=True) for e in existing if e.get("spec")]
    good, rejected = [], []
    for i, idea in enumerate(ideas[:MAX_PER_REQUEST], 1):
        if not isinstance(idea, dict):
            rejected.append(f"idea {i}: not an object")
            continue
        label = f"idea {i} ({str(idea.get('name') or 'unnamed')[:40]})"
        spec = idea.get("spec")
        if spec is None and "entry" in idea:  # spec fields put on the idea itself
            spec = {k: idea[k] for k in ("candle_seconds", "entry", "exit") if k in idea}
        try:
            spec = validate_spec(_coerce(spec))
        except SpecError as exc:
            rejected.append(f"{label}: {exc}")
            continue
        symbols = []
        for name in idea.get("symbols") or []:
            symbol = _market(name, allowed_symbols)
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        if not symbols:
            rejected.append(f"{label}: no allowed market (use {', '.join(allowed_symbols)})")
            continue
        key = json.dumps(spec, sort_keys=True)
        if key in seen:
            rejected.append(f"{label}: already tested")
            continue
        seen.append(key)
        good.append({"name": str(idea.get("name") or f"AI idea {i}")[:60],
                     "why": str(idea.get("why") or "")[:500], "symbols": symbols, "spec": spec})
    return good, rejected


class IdeaGenerator:
    def __init__(self, api_key: str, model: str = "openrouter/auto", daily_cap: int = 20,
                 client: httpx.AsyncClient | None = None, clock=time.time):
        self.api_key = api_key
        self.model = model or "openrouter/auto"
        self.daily_cap = daily_cap
        self.client = client
        self.clock = clock
        self._asked: list[float] = []

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def remaining_today(self) -> int:
        now = self.clock()
        self._asked = [t for t in self._asked if now - t < 86400]
        return max(self.daily_cap - len(self._asked), 0)

    async def ideas(self, count: int, symbols: list[str], entries: list[dict],
                    max_daily_loss: float) -> tuple[list[dict], list[str]]:
        if not self.configured:
            raise AIError("OPENROUTER_API_KEY is not set on the server")
        count = max(1, min(count, MAX_PER_REQUEST, self.remaining_today()))
        if not self.remaining_today():
            raise AIError(f"daily limit of {self.daily_cap} AI ideas reached")
        self._asked.extend([self.clock()] * count)
        messages = build_prompt(count, symbols, entries, max_daily_loss)
        client = self.client or httpx.AsyncClient(timeout=120)
        try:
            text = await self._ask(client, messages)
            good, rejected = parse_ideas(text, symbols, entries)
            if rejected and len(good) < count:
                # One repair round: show the model exactly what failed.
                log.info("AI ideas rejected, asking for fixes: %s", "; ".join(rejected))
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content":
                        "These ideas were rejected by the validator:\n- " + "\n- ".join(rejected) +
                        "\nReply with JSON only, in the same shape, containing corrected "
                        "versions of just the rejected ideas. Use only the rule types, "
                        "parameter names and ranges listed above, and plain JSON numbers "
                        "and true/false."},
                ]
                fixed, rejected = parse_ideas(await self._ask(client, messages), symbols,
                                              entries + [{"spec": g["spec"]} for g in good])
                good += fixed
        finally:
            if self.client is None:
                await client.aclose()
        if rejected:
            log.info("AI ideas rejected: %s", "; ".join(rejected))
        return good, rejected

    async def _ask(self, client: httpx.AsyncClient, messages: list[dict]) -> str:
        body = {"model": self.model, "temperature": 0.9, "messages": messages}
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Title": "Trading Bot"}
        try:
            resp = await client.post(URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise AIError(f"could not reach OpenRouter ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise AIError(f"OpenRouter answered {resp.status_code}")
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError("unexpected reply from OpenRouter") from None
