# Trading-Bot

A Python bot that trades Deriv **multipliers** over the Deriv API. It starts on
the demo account, enforces hard risk limits, and logs every trade. The trading
strategy is pluggable. Built in: `watch` (never trades), `demo_test` (demo-only
plumbing test), `trend` (moving-average crossover) and `mean_revert`
(Bollinger-band fade).

**New here? Follow [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md).**

## Quick start

```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # then fill in token and App ID
python -m bot --check                               # test connection, no trading
python -m bot                                       # run (watch-only by default)
```

All settings live in `.env` (see [.env.example](.env.example)). The token is
read from the environment only, never committed, and redacted from logs.

## Safety rails

| Rail | Setting | Default |
|---|---|---|
| Demo unless explicitly confirmed | `DERIV_ACCOUNT_TYPE`, `ALLOW_REAL_MONEY=YES_I_UNDERSTAND` | demo |
| Fixed stake, no martingale | `STAKE` (strategies cannot change it) | 1.00 |
| Max stake per trade | `MAX_STAKE` (also caps the quoted price) | 2.00 |
| Max daily loss, worst case incl. open trades | `MAX_DAILY_LOSS` | 5.00 |
| Max trades per day / open at once | `MAX_TRADES_PER_DAY`, `MAX_OPEN_TRADES` | 50 / 1 |
| Pause after a losing streak | `MAX_CONSECUTIVE_LOSSES`, `PAUSE_MINUTES` | 3 / 30 |
| Close trades open too long | `MAX_TRADE_MINUTES` | 10 |
| Kill switch: close trades and exit | create the file `STOP` | |
| Log decisions without buying | `DRY_RUN=true` | false |

Daily counters are saved in `state/risk_state.json` (UTC day), so a restart
does not reset the loss limit. On every (re)connect the bot reads the account's
open contracts and tracks them, so **use an account dedicated to the bot**.

## How it works

1. `GET /trading/v1/options/accounts` picks the active demo (or real) account.
2. `POST .../accounts/{id}/otp` returns a one-time WebSocket URL.
3. Over that socket: subscribe to `ticks`, and for each strategy signal that
   passes the risk check: `proposal` → `buy` → `proposal_open_contract`
   (subscribed) until `is_sold`, with `sell` for the time limit or kill switch.
4. If the socket drops, it fetches a fresh URL and reconnects with backoff.

REST calls send `Authorization: Bearer <token>` and `Deriv-App-ID`. Requests are
throttled to 2 per second, far under Deriv's published limits.

## Layout

```
bot/
  __main__.py      entry point (python -m bot, --check)
  backtest.py      python -m bot.backtest: replay past prices
  config.py        settings from .env, with validation
  deriv_client.py  REST + WebSocket client, req_id matching, throttle
  risk.py          hard limits and persisted daily state
  bot.py           main loop, trading, reconnect, kill switch
  trade_log.py     logs/bot.log and logs/trades.csv
  strategies/      base.py (interface), watch.py, demo_test.py, trend.py, mean_revert.py
tests/             offline tests, incl. a fake Deriv WebSocket server
```

## Strategies

| `STRATEGY=` | What it does |
|---|---|
| `watch` | Logs prices, never trades (default). |
| `demo_test` | One tiny alternating trade every few minutes to prove the plumbing. Demo only. |
| `trend` | Groups prices into candles (`TREND_CANDLE_SECONDS`, default 60) and trades UP when the fast EMA (`TREND_FAST`, 9) crosses above the slow EMA (`TREND_SLOW`, 21), DOWN when it crosses below. Exits come from `MAX_TRADE_MINUTES`, `TAKE_PROFIT` and `STOP_LOSS`. |
| `mean_revert` | Bollinger bands of `MR_PERIOD` (20) candles at `MR_STD` (2.0) standard deviations, on `MR_CANDLE_SECONDS` (60) candles. Signals UP on the first close below the lower band and DOWN on the first close above the upper band, betting on a snap back. Same exits as `trend`. |

## Backtesting

```
python -m bot.backtest --strategy trend                 # ~2 weeks of 1-minute candles
python -m bot.backtest --strategy trend --symbol frxEURUSD
python -m bot.backtest --file data/1HZ100V_60.csv       # rerun on saved prices
```

Downloads candles from Deriv's public feed (no token needed), replays them
through the strategy and the same `RiskManager` the live bot uses, and prints
win rate, break-even win rate, average win/loss, net result and worst
drawdown. Every simulated trade goes to `logs/backtest_<strategy>_<symbol>.csv`.
P/L is modelled as stake x multiplier x price move, capped at losing the stake,
and is **before Deriv's commission and spread** (`--commission` adds a cost).
When a candle touches both a loss exit and a profit exit, the loss is assumed.

## Adding a strategy

Subclass `Strategy` in `bot/strategies/`, return `Signal("UP" | "DOWN", reason)`
from `on_tick` when you want a trade, and register it in
`bot/strategies/__init__.py`. Then set `STRATEGY=<name>`. The strategy only
decides direction and timing; stake and limits stay with the risk manager.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

## Not verified yet

Written against the docs at developers.deriv.com, but not yet run against a
live Deriv account. Things to watch on the first demo run:
- Allowed `MULTIPLIER` values differ per symbol. If Deriv rejects 40, its error
  message lists the allowed values.
- Field names in `buy` and `proposal_open_contract` responses (`contract_id`,
  `buy_price`, `is_sold`, `profit`) follow the long-standing Deriv API.
