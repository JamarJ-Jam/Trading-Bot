"""Rule-based strategies built in the app's strategy builder (or proposed by
the AI), described as plain data instead of code.

A spec looks like this:

    {
      "candle_seconds": 900,
      "entry": [
        {"type": "breakout", "lookback": 8},
        {"type": "price_vs_ema", "length": 50},
        {"type": "session", "start_hour": 7, "end_hour": 12}
      ],
      "exit": {"stop_loss": 0.5, "take_profit": 1.0,
               "max_trade_minutes": 480, "exit_on_reverse": false}
    }

Every direction rule votes UP, DOWN or nothing on each closed candle;
filters (session) only allow or block. The strategy wants a direction when
all direction rules agree and every filter allows it, and signals when that
wanted direction changes. Like every strategy it only picks a direction:
the stake and the risk limits stay with the bot.
"""

from __future__ import annotations

import math
from collections import deque

from .base import Signal, Strategy

CANDLE_SIZES = (60, 300, 900, 1800, 3600, 14400, 86400)
MAX_RULES = 6
MAX_LENGTH = 500

# type -> {param: (kind, min, max, default)}; "direction" rules vote, "filter" rules gate.
RULES: dict[str, dict] = {
    "ema_cross": {
        "role": "direction",
        "label": "Fast average above or below the slow average",
        "params": {"fast": ("int", 2, MAX_LENGTH, 9), "slow": ("int", 3, MAX_LENGTH, 21)},
    },
    "price_vs_ema": {
        "role": "direction",
        "label": "Price above or below its moving average",
        "params": {"length": ("int", 2, MAX_LENGTH, 50)},
    },
    "breakout": {
        "role": "direction",
        "label": "Price breaks the high or low of the last N candles",
        "params": {"lookback": ("int", 2, MAX_LENGTH, 20)},
    },
    "momentum": {
        "role": "direction",
        "label": "Price higher or lower than N candles ago",
        "params": {"lookback": ("int", 1, MAX_LENGTH, 10)},
    },
    "rsi": {
        "role": "direction",
        "label": "RSI oversold (buy) or overbought (sell)",
        "params": {"length": ("int", 2, 100, 14), "low": ("float", 1, 49, 30),
                   "high": ("float", 51, 99, 70)},
    },
    "bollinger": {
        "role": "direction",
        "label": "Price closes outside its Bollinger band (bet on a snap back)",
        "params": {"period": ("int", 2, MAX_LENGTH, 20), "std": ("float", 0.5, 5, 2.0)},
    },
    "session": {
        "role": "filter",
        "label": "Only trade between these UTC hours",
        "params": {"start_hour": ("int", 0, 23, 7), "end_hour": ("int", 1, 24, 12)},
    },
}

EXIT_PARAMS = {
    "stop_loss": ("float", 0, 1000, None),
    "take_profit": ("float", 0, 1000, None),
    "max_trade_minutes": ("int", 0, 60 * 24 * 30, 0),
    "exit_on_reverse": ("bool", None, None, False),
}


class SpecError(ValueError):
    """Raised when a rule spec is malformed or out of range."""


def _number(value, kind: str, lo, hi, where: str):
    if kind == "bool":
        if not isinstance(value, bool):
            raise SpecError(f"{where} must be true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{where} must be a number")
    if kind == "int":
        if value != int(value):
            raise SpecError(f"{where} must be a whole number")
        value = int(value)
    else:
        value = float(value)
        if not math.isfinite(value):
            raise SpecError(f"{where} must be a number")
    if not lo <= value <= hi:
        raise SpecError(f"{where} must be between {lo} and {hi}")
    return value


def validate_spec(spec) -> dict:
    """Check a spec and return a clean copy with defaults filled in.

    Anything unknown is rejected, so a spec from the builder or the AI can
    only ever use the rules listed above."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    unknown = set(spec) - {"candle_seconds", "entry", "exit"}
    if unknown:
        raise SpecError(f"unknown spec fields: {', '.join(sorted(unknown))}")
    candle = spec.get("candle_seconds")
    if candle not in CANDLE_SIZES:
        raise SpecError(f"candle_seconds must be one of {', '.join(map(str, CANDLE_SIZES))}")

    entry = spec.get("entry")
    if not isinstance(entry, list) or not entry:
        raise SpecError("entry needs at least one rule")
    if len(entry) > MAX_RULES:
        raise SpecError(f"entry can have at most {MAX_RULES} rules")
    clean_rules = []
    for i, rule in enumerate(entry, 1):
        if not isinstance(rule, dict) or rule.get("type") not in RULES:
            raise SpecError(f"rule {i} must have a type: {', '.join(RULES)}")
        meta = RULES[rule["type"]]
        extra = set(rule) - {"type"} - set(meta["params"])
        if extra:
            raise SpecError(f"rule {i} ({rule['type']}) has unknown fields: {', '.join(sorted(extra))}")
        clean = {"type": rule["type"]}
        for name, (kind, lo, hi, default) in meta["params"].items():
            clean[name] = _number(rule.get(name, default), kind, lo, hi, f"rule {i} {name}")
        clean_rules.append(clean)
    if not any(RULES[r["type"]]["role"] == "direction" for r in clean_rules):
        raise SpecError("entry needs at least one rule that picks a direction")
    for r in clean_rules:
        if r["type"] == "ema_cross" and r["fast"] >= r["slow"]:
            raise SpecError("ema_cross fast must be smaller than slow")
        if r["type"] == "session" and r["start_hour"] >= r["end_hour"]:
            raise SpecError("session start_hour must be before end_hour")

    exits = spec.get("exit", {})
    if not isinstance(exits, dict):
        raise SpecError("exit must be an object")
    extra = set(exits) - set(EXIT_PARAMS)
    if extra:
        raise SpecError(f"exit has unknown fields: {', '.join(sorted(extra))}")
    clean_exit = {}
    for name, (kind, lo, hi, default) in EXIT_PARAMS.items():
        value = exits.get(name, default)
        clean_exit[name] = None if value is None else _number(value, kind, lo, hi, f"exit {name}")
    for name in ("stop_loss", "take_profit"):
        if clean_exit[name] == 0:
            clean_exit[name] = None
    if not (clean_exit["stop_loss"] or clean_exit["take_profit"]
            or clean_exit["max_trade_minutes"] or clean_exit["exit_on_reverse"]):
        raise SpecError("exit needs at least one way out: stop loss, take profit, "
                        "a time limit, or exit on reverse")
    return {"candle_seconds": candle, "entry": clean_rules, "exit": clean_exit}


def describe_spec(spec: dict) -> str:
    """One plain-English line per rule, for the app and the AI prompt."""
    parts = []
    for r in spec["entry"]:
        t = r["type"]
        if t == "ema_cross":
            parts.append(f"EMA{r['fast']} above/below EMA{r['slow']}")
        elif t == "price_vs_ema":
            parts.append(f"price above/below EMA{r['length']}")
        elif t == "breakout":
            parts.append(f"break of the {r['lookback']}-candle high/low")
        elif t == "momentum":
            parts.append(f"price up/down over {r['lookback']} candles")
        elif t == "rsi":
            parts.append(f"RSI{r['length']} below {r['low']:g} / above {r['high']:g}")
        elif t == "bollinger":
            parts.append(f"close outside Bollinger({r['period']}, {r['std']:g})")
        elif t == "session":
            parts.append(f"only {r['start_hour']:02d}:00-{r['end_hour']:02d}:00 UTC")
    return " and ".join(parts)


def _warmup(rules: list[dict]) -> int:
    need = 1
    for r in rules:
        need = max(need, r.get("slow", 0), r.get("length", 0), r.get("lookback", 0) + 1,
                   r.get("period", 0) + 1)
    # EMAs need a few lengths of history to settle.
    if any(r["type"] in ("ema_cross", "price_vs_ema") for r in rules):
        need *= 3
    return min(need + 2, 2000)


class _Rule:
    def update(self, close: float, hour: int):
        """Feed one closed candle; return "UP", "DOWN", None (no vote) or,
        for filters, True/False."""
        raise NotImplementedError


class _Ema:
    def __init__(self, length: int):
        self.alpha, self.value, self.count = 2 / (length + 1), None, 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.alpha * (x - self.value)
        self.count += 1
        return self.value


class _EmaCross(_Rule):
    def __init__(self, fast: int, slow: int):
        self.fast, self.slow, self.need = _Ema(fast), _Ema(slow), slow

    def update(self, close, hour):
        diff = self.fast.update(close) - self.slow.update(close)
        if self.slow.count < self.need:
            return None
        return "UP" if diff > 0 else "DOWN" if diff < 0 else None


class _PriceVsEma(_Rule):
    def __init__(self, length: int):
        self.ema, self.need = _Ema(length), length

    def update(self, close, hour):
        avg = self.ema.update(close)
        if self.ema.count < self.need:
            return None
        return "UP" if close > avg else "DOWN" if close < avg else None


class _Breakout(_Rule):
    def __init__(self, lookback: int):
        self.prev: deque[float] = deque(maxlen=lookback)

    def update(self, close, hour):
        vote = None
        if len(self.prev) == self.prev.maxlen:
            if close > max(self.prev):
                vote = "UP"
            elif close < min(self.prev):
                vote = "DOWN"
        self.prev.append(close)
        return vote


class _Momentum(_Rule):
    def __init__(self, lookback: int):
        self.prev: deque[float] = deque(maxlen=lookback)

    def update(self, close, hour):
        vote = None
        if len(self.prev) == self.prev.maxlen:
            old = self.prev[0]
            vote = "UP" if close > old else "DOWN" if close < old else None
        self.prev.append(close)
        return vote


class _Rsi(_Rule):
    """Wilder's RSI. Oversold votes UP (expecting a bounce), overbought DOWN."""

    def __init__(self, length: int, low: float, high: float):
        self.length, self.low, self.high = length, low, high
        self.last: float | None = None
        self.gain = self.loss = 0.0
        self.count = 0

    def update(self, close, hour):
        if self.last is None:
            self.last = close
            return None
        change, self.last = close - self.last, close
        up, down = max(change, 0.0), max(-change, 0.0)
        self.count += 1
        if self.count <= self.length:
            self.gain += up / self.length
            self.loss += down / self.length
            if self.count < self.length:
                return None
        else:
            self.gain = (self.gain * (self.length - 1) + up) / self.length
            self.loss = (self.loss * (self.length - 1) + down) / self.length
        rsi = 100.0 if self.loss == 0 else 100 - 100 / (1 + self.gain / self.loss)
        return "UP" if rsi < self.low else "DOWN" if rsi > self.high else None


class _Bollinger(_Rule):
    def __init__(self, period: int, std: float):
        self.closes: deque[float] = deque(maxlen=period)
        self.width = std

    def update(self, close, hour):
        vote = None
        if len(self.closes) == self.closes.maxlen:
            n = len(self.closes)
            mean = sum(self.closes) / n
            sd = math.sqrt(sum((c - mean) ** 2 for c in self.closes) / n)
            if close < mean - self.width * sd:
                vote = "UP"
            elif close > mean + self.width * sd:
                vote = "DOWN"
        self.closes.append(close)
        return vote


class _Session(_Rule):
    def __init__(self, start_hour: int, end_hour: int):
        self.start, self.end = start_hour, end_hour

    def update(self, close, hour):
        return self.start <= hour < self.end


_BUILDERS = {
    "ema_cross": _EmaCross, "price_vs_ema": _PriceVsEma, "breakout": _Breakout,
    "momentum": _Momentum, "rsi": _Rsi, "bollinger": _Bollinger, "session": _Session,
}


class RuleStrategy(Strategy):
    name = "rules"

    def __init__(self, spec: dict, label: str = "rules"):
        self.spec = validate_spec(spec)
        self.name = label
        self.candle_seconds = self.spec["candle_seconds"]
        self.warmup_candles = _warmup(self.spec["entry"])
        self._rules = []
        for r in self.spec["entry"]:
            params = {k: v for k, v in r.items() if k != "type"}
            self._rules.append((RULES[r["type"]]["role"], _BUILDERS[r["type"]](**params)))
        self._bucket: int | None = None
        self._close: float | None = None
        self._want: str | None = None

    def on_tick(self, price: float, epoch: int) -> Signal | None:
        bucket = epoch // self.candle_seconds
        signal = None
        if self._bucket is not None and bucket != self._bucket:
            signal = self._on_candle_close(self._bucket * self.candle_seconds, self._close)
        self._bucket, self._close = bucket, price
        return signal

    def current_direction(self) -> str | None:
        return self._want

    def _on_candle_close(self, start: int, close: float) -> Signal | None:
        hour = (start % 86400) // 3600
        votes, allowed = set(), True
        for role, rule in self._rules:
            result = rule.update(close, hour)  # every rule sees every candle
            if role == "filter":
                allowed = allowed and bool(result)
            else:
                votes.add(result)
        want = votes.pop() if len(votes) == 1 and allowed else None
        prev, self._want = self._want, want
        if want is not None and want != prev:
            return Signal(want, describe_spec(self.spec))
        return None


def apply_exits(cfg, spec: dict):
    """Return a copy of a Config using the spec's exits instead of the .env ones."""
    from dataclasses import replace

    exits = validate_spec(spec)["exit"]
    return replace(cfg, stop_loss=exits["stop_loss"], take_profit=exits["take_profit"],
                   max_trade_minutes=exits["max_trade_minutes"],
                   exit_on_reverse=exits["exit_on_reverse"])
