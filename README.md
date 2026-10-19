# Trading-Bot

A Python bot that trades Deriv **multipliers** over the Deriv API. It starts on
the demo account, enforces hard risk limits, and logs every trade. The trading
strategy is pluggable. Built in: `watch` (never trades), `demo_test` (demo-only
plumbing test), `trend` (moving-average crossover), `mean_revert`
(Bollinger-band fade) and `breakout` (London-open session breakout).

**New here? Follow [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md).**

## Phone app

`python -m server` runs the bot together with a phone web app: start, pause
and stop the bot, change strategy and limits, switch between demo and real,
see every trade, and test new strategy ideas in a **strategy lab** (backtest,
unseen data, 14 days on live prices, then a "ready to go live" alert). Ideas
come from a no-code rule builder or from an AI model through OpenRouter. It
is meant to run in Docker on an always-on machine behind a Cloudflare Tunnel:
**see [docs/APP_GUIDE.md](docs/APP_GUIDE.md).**

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
| Close trades open too long | `MAX_TRADE_MINUTES` (0 = no limit) | 10 |
| Close a trade when the strategy signals the other way, and reopen in the trend's direction after a Deriv expiry or restart | `EXIT_ON_REVERSE=true` | false |
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
  strategies/      base.py (interface), watch.py, demo_test.py, trend.py, mean_revert.py,
                   breakout.py, rules.py (strategies built in the app or by the AI)
server/
  __main__.py      python -m server (the app), python -m server setup (login)
  api.py           JSON API and static files
  auth.py          password + authenticator-app code, signed session cookie
  settings.py      app settings layered over .env, real-money switch
  runner.py        runs the bot inside the server: start, pause, stop all
  lab.py           strategy lab pipeline and readiness checks
  pipeline.py      background lab testing, paper trading, alerts
  ai.py            AI strategy ideas through OpenRouter
web/               the phone app (plain HTML, CSS and JavaScript)
tests/             offline tests, incl. a fake Deriv WebSocket server
```

## Strategies

| `STRATEGY=` | What it does |
|---|---|
| `watch` | Logs prices, never trades (default). |
| `demo_test` | One tiny alternating trade every few minutes to prove the plumbing. Demo only. |
| `trend` | Groups prices into candles (`TREND_CANDLE_SECONDS`, default 60) and trades UP when the fast EMA (`TREND_FAST`, 9) crosses above the slow EMA (`TREND_SLOW`, 21), DOWN when it crosses below. Exits come from `MAX_TRADE_MINUTES`, `TAKE_PROFIT` and `STOP_LOSS`. |
| `mean_revert` | Bollinger bands of `MR_PERIOD` (20) candles at `MR_STD` (2.0) standard deviations, on `MR_CANDLE_SECONDS` (60) candles. Signals UP on the first close below the lower band and DOWN on the first close above the upper band, betting on a snap back. Same exits as `trend`. |
| `breakout` | Marks the Asian range: the highest and lowest 15-minute close (`BO_CANDLE_SECONDS`, 900) between `BO_RANGE_START` (0) and `BO_RANGE_END` (7) UTC. Until `BO_TRADE_END` (12) UTC, the first close above the range signals UP and below it DOWN. One trade a day at most. Meant for forex; pair it with `MAX_TRADE_MINUTES=480`. |

`trend`, `mean_revert` and `breakout` preload their recent candles from Deriv at startup,
so they can trade straight away even on hourly candles. The candle size must
be one Deriv offers (60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400,
28800 or 86400 seconds); otherwise the bot builds history from live prices.

## Backtesting

```
python -m bot.backtest --strategy trend                 # ~2 weeks of 1-minute candles
python -m bot.backtest --strategy trend --symbol frxEURUSD
python -m bot.backtest --file data/1HZ100V_60.csv       # rerun on saved prices
MAX_TRADE_MINUTES=480 python -m bot.backtest --strategy breakout --granularity 900 \
  --symbol frxEURUSD,frxGBPUSD,frxUSDJPY,frxAUDUSD --commission 0.0002
```

Several symbols separated by commas each get their own report, then a
combined total.

Every run also writes `logs/backtest_<strategy>_<kind>.html`: a chart of each
symbol's running profit with a hover readout. Open it in a browser (from WSL:
`explorer.exe logs/backtest_trend_multiplier.html`).

`--payout 0.95` simulates Rise/Fall contracts instead of multipliers: the same
signals, each a fixed bet that pays 95% of the stake if the price is on the
right side after `MAX_TRADE_MINUTES`, and loses the stake otherwise.

Deriv only serves about a year of daily candles. For long daily tests,
`--source fred` downloads decades of free daily rates from the US Federal
Reserve (FRED) for the major pairs, plus Bitcoin and Ethereum (`cryBTCUSD`,
`cryETHUSD`, from about 2015), instead (one rate per day, so stop-outs
during the day aren't seen):

```
TREND_CANDLE_SECONDS=86400 MAX_TRADE_MINUTES=0 EXIT_ON_REVERSE=true MULTIPLIER=10 \
  python -m bot.backtest --strategy trend --source fred --granularity 86400 \
  --symbol frxEURUSD,frxGBPUSD,frxUSDJPY,frxAUDUSD --commission 0.0002
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
