"""The markets the app offers in its Market drop-down.

Names differ by broker: the Deriv API uses codes such as `cryETHUSD`, while
MetaTrader 5 uses the names shown in its Market Watch. The lists are a
starting point; the app also lets you type any other symbol, and you can
replace a whole list with MARKETS_DERIV or MARKETS_MT5 in .env
(`code:Label,code:Label`; the label is optional).
"""

from __future__ import annotations

DERIV = [
    ("Crypto", [("cryBTCUSD", "Bitcoin (BTC/USD)"), ("cryETHUSD", "Ethereum (ETH/USD)"),
                ("cryLTCUSD", "Litecoin (LTC/USD)")]),
    ("Volatility indices", [
        ("1HZ10V", "Volatility 10 (1s)"), ("1HZ25V", "Volatility 25 (1s)"),
        ("1HZ50V", "Volatility 50 (1s)"), ("1HZ75V", "Volatility 75 (1s)"),
        ("1HZ100V", "Volatility 100 (1s)"), ("R_10", "Volatility 10"),
        ("R_25", "Volatility 25"), ("R_50", "Volatility 50"),
        ("R_75", "Volatility 75"), ("R_100", "Volatility 100")]),
    ("Forex", [("frxEURUSD", "EUR/USD"), ("frxGBPUSD", "GBP/USD"), ("frxUSDJPY", "USD/JPY"),
               ("frxAUDUSD", "AUD/USD"), ("frxUSDCAD", "USD/CAD"), ("frxUSDCHF", "USD/CHF"),
               ("frxEURGBP", "EUR/GBP"), ("frxEURJPY", "EUR/JPY")]),
    ("Metals", [("frxXAUUSD", "Gold (XAU/USD)"), ("frxXAGUSD", "Silver (XAG/USD)")]),
]

MT5 = [
    ("Crypto", [("BTCUSD", "Bitcoin (BTC/USD)"), ("ETHUSD", "Ethereum (ETH/USD)"),
                ("LTCUSD", "Litecoin (LTC/USD)")]),
    ("Derived indices", [
        ("Volatility 10 (1s) Index", "Volatility 10 (1s)"), ("Volatility 25 (1s) Index", "Volatility 25 (1s)"),
        ("Volatility 50 (1s) Index", "Volatility 50 (1s)"), ("Volatility 75 (1s) Index", "Volatility 75 (1s)"),
        ("Volatility 100 (1s) Index", "Volatility 100 (1s)"), ("Volatility 100 Index", "Volatility 100"),
        ("Boom 1000 Index", "Boom 1000"), ("Crash 1000 Index", "Crash 1000"),
        ("Step Index", "Step Index")]),
    ("Forex", [("EURUSD", "EUR/USD"), ("GBPUSD", "GBP/USD"), ("USDJPY", "USD/JPY"),
               ("AUDUSD", "AUD/USD"), ("USDCAD", "USD/CAD"), ("USDCHF", "USD/CHF"),
               ("EURGBP", "EUR/GBP"), ("EURJPY", "EUR/JPY")]),
    ("Metals", [("XAUUSD", "Gold (XAU/USD)"), ("XAGUSD", "Silver (XAG/USD)")]),
]


def parse_override(text: str) -> list:
    """`code:Label,code` -> one group called "My markets"."""
    items = []
    for part in (text or "").split(","):
        code, _, label = part.strip().partition(":")
        code = code.strip()
        if code:
            items.append((code, label.strip() or code))
    return [("My markets", items)] if items else []


def markets_for(broker: str, env: dict | None = None) -> list[dict]:
    env = env or {}
    custom = parse_override(env.get("MARKETS_MT5" if broker == "mt5" else "MARKETS_DERIV", ""))
    groups = custom or (MT5 if broker == "mt5" else DERIV)
    return [{"group": g, "items": [{"value": v, "label": lbl} for v, lbl in items]}
            for g, items in groups]
