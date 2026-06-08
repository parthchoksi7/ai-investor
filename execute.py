"""
execute.py — Sends trade orders to Robinhood and fetches portfolio state.

Targets the Robinhood Agentic account exclusively via ROBINHOOD_ACCOUNT_NUMBER.
Set DRY_RUN=true to log decisions without placing any real orders.
"""

import os
import csv
import pyotp
import robin_stocks.robinhood as rh
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TRADE_LOG = "trades.csv"
DRY_RUN   = os.getenv("DRY_RUN", "false").lower() == "true"
AGENTIC_ACCOUNT = os.getenv("ROBINHOOD_ACCOUNT_NUMBER")

# Hard-blocked tickers — never bought or sold under any circumstances
BLOCKED_TICKERS = {"TSLA"}

_logged_in = False


def _login():
    global _logged_in
    if _logged_in:
        return

    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")
    if not username or not password:
        raise EnvironmentError("ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD must be set.")

    mfa_code   = None
    mfa_secret = os.getenv("ROBINHOOD_MFA_SECRET")
    if mfa_secret:
        mfa_code = pyotp.TOTP(mfa_secret.strip()).now()

    rh.login(username=username, password=password, store_session=True, mfa_code=mfa_code)
    _logged_in = True

    if AGENTIC_ACCOUNT:
        print(f"   🔒 Locked to Robinhood Agentic account: {AGENTIC_ACCOUNT}")
    else:
        print("   ⚠️  ROBINHOOD_ACCOUNT_NUMBER not set — targeting default account.")


def get_trade_history(n: int = 30) -> list[dict]:
    """Reads the last n rows from trades.csv."""
    if not os.path.isfile(TRADE_LOG):
        return []
    with open(TRADE_LOG, newline="") as f:
        return list(csv.DictReader(f))[-n:]


def _compute_qty(
    target_weight: float,
    action: str,
    ticker: str,
    portfolio: dict,
    prices: dict,
) -> float:
    """Convert a target portfolio weight to a share count (fractional) to buy or sell."""
    current_price = prices.get(ticker, {}).get("close", 0)
    if not current_price:
        return 0.0

    total_value    = portfolio["total_value"]
    target_dollars = target_weight * total_value

    current_qty = 0.0
    for p in portfolio["positions"]:
        if p["symbol"] == ticker:
            current_qty = float(p["qty"])
            break

    current_dollars = current_qty * current_price
    delta_dollars   = target_dollars - current_dollars

    if action == "BUY":
        if delta_dollars <= 0:
            return 0.0  # already at or above target weight
        return round(delta_dollars / current_price, 6)

    if action == "SELL":
        if target_weight == 0:
            return current_qty  # exit entire position
        if delta_dollars >= 0:
            return 0.0  # already at or below target weight
        return round(abs(delta_dollars) / current_price, 6)

    return 0.0


def log_trades(decisions: list[dict], portfolio: dict, prices: dict | None = None, strategy: str = "institutional") -> None:
    """Appends executed trades to trades.csv."""
    file_exists = os.path.isfile(TRADE_LOG)
    fieldnames  = ["date", "strategy", "ticker", "action", "qty", "price", "total_value", "target_weight", "portfolio_value", "rationale"]

    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()

        today = datetime.now().strftime("%Y-%m-%d")
        for trade in decisions:
            action = trade.get("action", "").upper()
            if action == "HOLD":
                continue
            ticker        = trade.get("ticker", "")
            target_weight = trade.get("target_weight")
            qty           = trade.get("qty")
            price         = prices.get(ticker, {}).get("close", 0) if prices else 0

            if qty is None and target_weight is not None and prices:
                qty = _compute_qty(target_weight, action, ticker, portfolio, prices)

            total_value = round(qty * price, 2) if qty and price else None

            writer.writerow({
                "date":            today,
                "strategy":        strategy,
                "ticker":          ticker,
                "action":          action,
                "qty":             qty or "",
                "price":           f"{price:.4f}" if price else "",
                "total_value":     f"{total_value:.2f}" if total_value is not None else "",
                "target_weight":   f"{target_weight:.4f}" if target_weight is not None else "",
                "portfolio_value": f"{portfolio['total_value']:.2f}",
                "rationale":       trade.get("rationale", ""),
            })

    print(f"   📝 Trades logged to {TRADE_LOG}")


def get_portfolio_summary() -> dict:
    """Fetches balance and positions from the Robinhood Agentic account.
    If mcp_portfolio.json exists, reads from it instead (used by the cloud routine).
    """
    if os.path.isfile("mcp_portfolio.json"):
        import json as _json
        with open("mcp_portfolio.json") as f:
            return _json.load(f)
    _login()

    account          = rh.profiles.load_account_profile(account_number=AGENTIC_ACCOUNT)
    portfolio_profile = rh.profiles.load_portfolio_profile(account_number=AGENTIC_ACCOUNT)
    holdings         = rh.account.build_holdings(account_number=AGENTIC_ACCOUNT) or {}

    cash        = float(account.get("cash", 0) or 0)
    total_equity = float(portfolio_profile.get("equity", 0) or 0)

    positions = []
    for ticker, data in holdings.items():
        qty          = float(data.get("quantity", 0) or 0)
        avg_price    = float(data.get("average_buy_price", 0) or 0)
        current_price = float(data.get("price", 0) or 0)
        market_value = float(data.get("equity", 0) or 0)
        positions.append({
            "symbol":         ticker,
            "qty":            qty,
            "avg_price":      avg_price,
            "current_price":  current_price,
            "market_value":   market_value,
            "unrealized_pnl": market_value - (avg_price * qty),
        })

    return {
        "cash":        cash,
        "total_value": total_equity,
        "positions":   positions,
    }


def place_order(ticker: str, action: str, qty: float) -> dict:
    """Places a market order on the Robinhood Agentic account."""
    if ticker in BLOCKED_TICKERS:
        print(f"   🚫 BLOCKED: {ticker} is on the hard-block list — order rejected")
        return {"blocked": True}

    if DRY_RUN:
        print(f"   🔵 [DRY RUN] Would place: {action} {qty}x {ticker}")
        return {"dry_run": True}

    if action.upper() == "BUY":
        result = rh.orders.order_buy_market(ticker, qty, account_number=AGENTIC_ACCOUNT)
    else:
        result = rh.orders.order_sell_market(ticker, qty, account_number=AGENTIC_ACCOUNT)

    if result and result.get("id"):
        print(f"   ✅ Order placed: {action} {qty}x {ticker} (id: {result['id']})")
    else:
        error = result.get("detail", result) if result else "no response"
        print(f"   ❌ Order failed for {ticker}: {error}")

    return result or {}


def execute_trades(decisions: list[dict], portfolio: dict, prices: dict) -> None:
    """Execute trade decisions. Converts target_weight to share count before ordering."""
    if not decisions:
        print("   Nothing to execute.")
        return

    if DRY_RUN:
        print("   ⚠️  DRY_RUN=true — decisions logged but no orders placed.")

    for trade in decisions:
        action = trade.get("action", "").upper()
        ticker = trade.get("ticker", "")

        if action == "HOLD" or not ticker:
            continue

        # Resolve quantity
        if "target_weight" in trade:
            qty = _compute_qty(trade["target_weight"], action, ticker, portfolio, prices)
        else:
            qty = int(trade.get("qty", 0))

        if qty <= 0:
            print(f"   ⏸  Skipping {action} {ticker} — computed qty {qty} ≤ 0")
            continue

        place_order(ticker, action, qty)
