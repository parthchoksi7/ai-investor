"""
execute.py — Sends trade orders to Alpaca and fetches portfolio state.
"""

import os
import csv
import requests
from datetime import datetime
from dotenv import load_dotenv

TRADE_LOG = "trades.csv"


def get_trade_history(n=30):
    """
    Reads the last n rows from trades.csv and returns them as a list of dicts.
    Returns an empty list if the file doesn't exist or is empty.
    """
    if not os.path.isfile(TRADE_LOG):
        return []

    with open(TRADE_LOG, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    return rows[-n:]


def log_trades(decisions, portfolio, strategy="institutional"):
    """
    Appends executed trades to trades.csv.
    Skips HOLD decisions (nothing was executed).
    """
    file_exists = os.path.isfile(TRADE_LOG)

    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "strategy", "ticker", "action", "qty", "portfolio_value", "rationale"])
        if not file_exists:
            writer.writeheader()

        date = datetime.now().strftime("%Y-%m-%d")
        for trade in decisions:
            if trade.get("action", "").upper() == "HOLD":
                continue
            writer.writerow({
                "date": date,
                "strategy": strategy,
                "ticker": trade.get("ticker", ""),
                "action": trade.get("action", "").upper(),
                "qty": trade.get("qty", 0),
                "portfolio_value": f"{portfolio['total_value']:.2f}",
                "rationale": trade.get("rationale", ""),
            })

    print(f"   📝 Trades logged to {TRADE_LOG}")

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")

# Paper trading URL — swap this for live when ready
BASE_URL = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type": "application/json"
}


def get_portfolio_summary():
    """
    Fetches current account balance and positions from Alpaca.
    Returns a clean summary dict.
    """
    # Get account info (cash, buying power)
    account_resp = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account = account_resp.json()

    # Get current positions
    positions_resp = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    raw_positions = positions_resp.json()

    positions = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            positions.append({
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "avg_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pnl": float(p["unrealized_pl"]),
            })

    cash = float(account.get("cash", 0))
    portfolio_value = float(account.get("portfolio_value", 0))

    return {
        "cash": cash,
        "total_value": portfolio_value,
        "positions": positions
    }


def place_order(ticker, action, qty):
    """
    Places a single market order on Alpaca.
    action: "BUY" or "SELL"
    """
    order = {
        "symbol": ticker,
        "qty": qty,
        "side": action.lower(),
        "type": "market",
        "time_in_force": "day"
    }

    resp = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=order)
    result = resp.json()

    if resp.status_code in (200, 201):
        print(f"   ✅ Order placed: {action} {qty}x {ticker} (order id: {result.get('id', '?')})")
    else:
        print(f"   ❌ Order failed for {ticker}: {result.get('message', result)}")

    return result


def execute_trades(decisions):
    """
    Takes Claude's list of trade decisions and executes each one.
    Skips HOLD decisions.
    """
    if not decisions:
        print("   Nothing to execute.")
        return

    for trade in decisions:
        action = trade.get("action", "").upper()
        ticker = trade.get("ticker", "")
        qty = trade.get("qty", 0)

        if action == "HOLD" or qty <= 0:
            print(f"   ⏸  HOLD {ticker} — skipping")
            continue

        place_order(ticker, action, qty)
