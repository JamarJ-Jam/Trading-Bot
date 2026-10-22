# Running the bot on MetaTrader 5

The bot can trade through a MetaTrader 5 (MT5) terminal instead of the Deriv
API. Same strategies, same limits (`STAKE`, `MAX_STAKE`, `MAX_DAILY_LOSS`,
the `STOP` kill switch, the losing-streak pause), same `logs/trades.csv`.
It starts on a **demo** account and refuses a real one unless you set
`ALLOW_REAL_MONEY=YES_I_UNDERSTAND`, exactly like the Deriv side.

## The one big difference: it runs on Windows, not WSL

MT5's official Python package (`MetaTrader5`) does not talk to the broker.
It talks to the **MT5 terminal app running on the same Windows machine**. So:

| Where | Deriv API bot | MT5 bot |
|---|---|---|
| WSL Ubuntu (your `~/Trading-Bot`) | works | **does not work** |
| Docker / Oracle (Linux) server | works | **does not work** |
| Windows Python (PowerShell) | works | works, with MT5 open |

Keep your WSL copy for Deriv and backtests. For MT5, make a second copy of
the code on the Windows side and run it from PowerShell. The MT5 app has to
stay open and logged in while the bot runs, so the PC must stay on (or use a
Windows VPS later).

## 1. Make a Deriv MT5 demo account

1. Log in to Deriv, open **Trader's Hub**, make sure **Demo** is selected.
2. Under **CFDs**, add an **MT5 Standard** (or Derived) demo account.
3. Deriv shows the **login number**, the **server** (for example
   `DerivSVG-Demo` or `Deriv-Demo`) and lets you set a password. Keep them in
   your password manager, not in chat.

## 2. Install MT5 and allow algo trading

1. Download MetaTrader 5 for Windows from Deriv's MT5 page and install it.
2. **File > Login to Trade Account**: enter the demo login, password, server.
3. **Tools > Options > Expert Advisors**: tick **Allow algorithmic trading**.
4. Make sure the **Algo Trading** button on the toolbar is green. When it is
   off, the bot still watches prices but every trade is refused.
5. In **Market Watch** (Ctrl+M), right-click > **Symbols** and note the exact
   name of what you want to trade, e.g. `Volatility 100 (1s) Index`,
   `EURUSD`, `XAUUSD`. Names differ from the Deriv API (`1HZ100V`, `frxEURUSD`).

## 3. Install Python on Windows and get the code

1. Install Python 3.12 from python.org (tick **Add python.exe to PATH**) and
   Git for Windows.
2. In PowerShell:

```powershell
cd $HOME
git clone https://github.com/JamarJ-Jam/Trading-Bot.git
cd Trading-Bot
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt     # installs MetaTrader5 on Windows only
copy .env.example .env
notepad .env
```

If PowerShell refuses to run `activate`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## 4. Settings in `.env`

```
BROKER=mt5
DERIV_ACCOUNT_TYPE=demo
SYMBOL=Volatility 100 (1s) Index
STRATEGY=watch
STAKE=1.00
MAX_STAKE=2.00
MAX_DAILY_LOSS=5.00
```

`DERIV_API_TOKEN` and `DERIV_APP_ID` are not needed for MT5.

**Login: leave `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` empty.** The bot
then uses whatever account the MT5 app is logged in to, and no password sits
in any file. Fill them in only if you want the bot to log in by itself
(the password is then in `.env`, which git ignores, and it is blanked out of
the logs).

Other MT5 settings (all optional):

| Setting | What it does | Default |
|---|---|---|
| `MT5_VOLUME` | lot size of every trade | the symbol's minimum |
| `MT5_MAGIC` | tag on the bot's positions; it ignores positions without it | 26102201 |
| `MT5_DEVIATION` | how far the price may move while an order fills, in points | 20 |
| `MT5_SERVER_UTC_OFFSET` | hours the broker's server clock is ahead of UTC | 0 |
| `MT5_PATH` | full path to `terminal64.exe`, only if you have several MT5 installs | |

`MULTIPLIER` does nothing on MT5.

## 5. How a trade works on MT5

On Deriv a multiplier trade can lose at most its stake. MT5 has no such
contract, so the bot builds the same thing:

* every trade is `MT5_VOLUME` lots (the smallest allowed by default);
* the bot puts a **stop loss where the loss would equal `STAKE`** (or
  `STOP_LOSS` if that is smaller), and a take profit at `TAKE_PROFIT` if set;
* that amount is what counts towards `MAX_DAILY_LOSS`, like a Deriv stake.

If the stake is too small for the lot size, the stop would sit inside the
spread, so the bot refuses with "stop loss is too close to the price; raise
STAKE or lower MT5_VOLUME". `python -m bot --check` shows what a trade would
look like before you run anything.

A stop loss is an order, not a guarantee. When the price jumps (weekend gap,
news), it fills at the next price, so a trade can lose somewhat more than its
stake. Commission and swap also come off the result. The daily loss limit
still uses the real results once trades close.

## 6. Test, then run on demo

```powershell
python -m bot --check
```

You should see the demo account, the latest price and a line like
"A buy now would be 0.5 lots with its stop loss at 998.10, risking 1.00".
Nothing is traded.

Then:

1. `STRATEGY=watch`, `python -m bot`: prices only, no trades.
2. `STRATEGY=demo_test`: one small trade every few minutes on demo, to see
   orders appear in MT5's **Trade** tab (comment `trading-bot`) and close.
3. Any other strategy, on demo, and compare with its backtest.

Stop it with the kill switch: create a file named `STOP` in the
`Trading-Bot` folder (`ni STOP` in PowerShell). The bot closes its positions
and exits. Ctrl+C stops the bot but leaves positions open with their stop
loss and take profit; the bot picks them up again on the next start. You can
always close them by hand in MT5.

Use an MT5 account (or at least a symbol) only for the bot. On a **netting**
account MT5 merges all positions on a symbol into one, so the bot needs
`MAX_OPEN_TRADES=1` there and would merge with a trade you place by hand.

## 7. The phone app with MT5

The app (`python -m server`) runs the bot inside it, so with `BROKER=mt5` it
also has to run on Windows, next to MT5. Docker and the Oracle server cannot
reach MT5.

1. In the Windows copy, add the three `APP_...` lines to `.env`
   (`python -m server setup` prints them).
2. `python -m server` starts it on http://127.0.0.1:8000.
3. To reach it from the phone, install `cloudflared` for Windows and point
   the tunnel at `http://localhost:8000` (same tunnel steps as in
   [APP_GUIDE.md](APP_GUIDE.md), just without Docker).

What changes in the app: Start, Pause, Stop all trading, the limits, the
trade list and the demo/real switch work as before. The strategy lab still
backtests and paper-trades on Deriv's public price feed (the MT5 symbol of
the same market moves the same way, but spreads differ). The Martingale test
is Deriv-only (Rise/Fall contracts don't exist on MT5) and is refused when
`BROKER=mt5`.

## Before real money

The same rule as on Deriv: costs on MT5 are the spread plus any commission,
and none of the strategies tested so far beat those costs. Backtest an idea
first, run it on the MT5 demo for at least two weeks, and only then decide.
