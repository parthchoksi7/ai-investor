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
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
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
    """Last n broker-accepted rows from trades.csv (the agent-facing history).

    Excludes dry_run=="True" rows: in the cloud, main.py logs every decision
    dry_run=True BEFORE the MCP orders are placed, and the reconciler
    (journal.mark_transactions_live) flips only broker-accepted rows to
    "False". A dry_run row is therefore either speculative (not yet
    reconciled) or a rejected order — the agents must never be told they
    hold a position the broker refused. (csv stores booleans as strings.)
    """
    if not os.path.isfile(TRADE_LOG):
        return []
    with open(TRADE_LOG, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("dry_run") != "True"]
    return rows[-n:]


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

    # Single lookup: held qty and the broker's sellable cap come from one pass.
    current_qty   = 0.0
    available_qty = 0.0
    for p in portfolio["positions"]:
        if p["symbol"] == ticker:
            current_qty   = float(p["qty"])
            available_qty = float(p.get("available_qty", p.get("qty", current_qty)))
            break

    current_dollars = current_qty * current_price
    delta_dollars   = target_dollars - current_dollars

    if action == "BUY":
        if delta_dollars <= 0:
            return 0.0  # already at or above target weight
        return round(delta_dollars / current_price, 6)

    if action == "SELL":
        # available_qty (shares_available_for_sells from broker) caps the sale
        if target_weight == 0:
            return available_qty  # exit entire position — never exceed available
        if delta_dollars >= 0:
            return 0.0  # already at or below target weight
        return round(min(abs(delta_dollars) / current_price, available_qty), 6)

    return 0.0


TRADE_LOG_FIELDS = [
    "date", "strategy", "ticker", "action", "qty", "price", "total_value",
    "target_weight", "portfolio_value", "rationale", "broker_order_id", "dry_run",
    "run_id",  # reconciliation key — journal.mark_transactions_live rewrites this run's rows against broker fills
]


def _migrate_trade_log() -> None:
    """Rewrite trades.csv under the current header if it carries an older schema.

    DictWriter never rewrites an existing header, so appending 12-field rows
    under an old 7-column header silently misaligns every new row.
    """
    if not os.path.isfile(TRADE_LOG):
        return
    with open(TRADE_LOG, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == TRADE_LOG_FIELDS:
            return
        rows = list(reader)
    tmp = TRADE_LOG + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in TRADE_LOG_FIELDS})
    os.replace(tmp, TRADE_LOG)
    print(f"   🔁 Migrated {TRADE_LOG} header to current schema ({len(rows)} row(s) preserved)")


def order_executed(result) -> bool:
    """True when a broker response represents a placed order.

    A placed order carries a broker order id (or is a dry run). Anything else —
    rejection detail, hard-block marker, empty/None response — is NOT a fill
    and must never be logged or reported as one.
    """
    return isinstance(result, dict) and bool(result.get("id") or result.get("dry_run"))


def log_trades(
    decisions: list[dict],
    portfolio: dict,
    prices: dict | None = None,
    strategy: str = "institutional",
    broker_order_ids: dict | None = None,
    run_id: str = "",
) -> None:
    """Appends executed trades to trades.csv."""
    _migrate_trade_log()
    file_exists = os.path.isfile(TRADE_LOG)
    fieldnames  = TRADE_LOG_FIELDS

    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()

        today = datetime.now(_ET).strftime("%Y-%m-%d")
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

            # Extract Robinhood order ID from execution results if available
            raw_result  = (broker_order_ids or {}).get(ticker, {})
            order_id    = raw_result.get("id", "") if isinstance(raw_result, dict) else ""

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
                "broker_order_id": order_id,
                "dry_run":         str(DRY_RUN),
                "run_id":          run_id,
            })

    print(f"   📝 Trades logged to {TRADE_LOG}")


class StalePortfolioError(Exception):
    """mcp_portfolio.json exists but is not provably from today (ET).

    Every order is sized from this file. A stale copy (a prior day's portfolio
    committed to the repo, or a routine that failed to refresh it) would size
    today's trades against the wrong cash/positions. Fail loud, like the
    market-data preflight abort, rather than trade on stale state.
    """


def get_portfolio_summary() -> dict:
    """Fetches balance and positions from the Robinhood Agentic account.
    If mcp_portfolio.json exists, reads from it instead (used by the cloud routine).

    The cloud file MUST carry an "as_of" ISO timestamp dated today (ET). A
    missing or stale as_of raises StalePortfolioError — main.py catches it,
    records portfolio: FAILED, and aborts before sizing any orders.
    """
    if os.path.isfile("mcp_portfolio.json"):
        import json as _json
        with open("mcp_portfolio.json") as f:
            data = _json.load(f)

        as_of = data.get("as_of")
        if not as_of:
            raise StalePortfolioError(
                "mcp_portfolio.json has no 'as_of' timestamp — cannot prove it is "
                "today's portfolio. The routine STEP 1 must write as_of (ISO, ET). "
                "Refusing to size orders against unverifiable portfolio state."
            )
        try:
            _dt = datetime.fromisoformat(as_of)
            # Spec says ET. A naive timestamp is assumed ET; an aware one is
            # converted to ET before taking the calendar date.
            _dt = _dt.replace(tzinfo=_ET) if _dt.tzinfo is None else _dt.astimezone(_ET)
            as_of_date = _dt.date()
        except (ValueError, TypeError) as e:
            raise StalePortfolioError(
                f"mcp_portfolio.json 'as_of' is unparseable ({as_of!r}): {e}. "
                "Cannot verify freshness — refusing to size orders."
            )
        today = datetime.now(_ET).date()
        if as_of_date != today:
            raise StalePortfolioError(
                f"mcp_portfolio.json is stale: as_of={as_of} (ET date {as_of_date}), "
                f"today is {today}. The routine did not refresh the portfolio. "
                "Refusing to size orders against a prior day's state."
            )
        return data
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


def execute_trades(decisions: list[dict], portfolio: dict, prices: dict) -> dict[str, dict]:
    """Execute trade decisions. Returns {ticker: broker_result} for reconciliation."""
    order_results: dict[str, dict] = {}

    if not decisions:
        print("   Nothing to execute.")
        return order_results

    if DRY_RUN:
        print("   ⚠️  DRY_RUN=true — decisions logged but no orders placed.")

    # SELLs first: this is a cash account, so a BUY funded by a same-day SELL
    # is rejected by the broker if it lands before the sale proceeds exist.
    ordered = sorted(
        decisions,
        key=lambda d: 0 if d.get("action", "").upper() == "SELL" else 1,
    )

    for trade in ordered:
        action = trade.get("action", "").upper()
        ticker = trade.get("ticker", "")

        if action == "HOLD" or not ticker:
            continue

        # Resolve quantity — prefer pre-computed fractional qty, fall back to weight-based calc.
        # Never round to whole shares; Robinhood supports fractional orders.
        if trade.get("qty") is not None:
            qty = float(trade["qty"])
        elif "target_weight" in trade:
            qty = _compute_qty(trade["target_weight"], action, ticker, portfolio, prices)
        else:
            qty = 0.0

        if qty <= 0:
            print(f"   ⏸  Skipping {action} {ticker} — qty {qty:.6f} ≤ 0")
            continue

        # Isolate each order: a transient exception on one must not abort the
        # loop and strand the rest. With SELL-before-BUY ordering the bad case
        # is SELLs placed, exception, BUYs never attempted → capital stranded
        # in cash. order_executed() classifies {"exception": True} as not-a-fill.
        try:
            result = place_order(ticker, action, qty)
        except Exception as e:
            print(f"   ❌ Order EXCEPTION for {ticker}: {e}")
            result = {"detail": str(e)[:200], "exception": True}
        order_results[ticker] = result

    return order_results
