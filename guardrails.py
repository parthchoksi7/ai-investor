"""
guardrails.py — Deterministic validation gate on LLM trade output.

Every decision the Portfolio Manager / CRO pipeline emits passes through
validate_decisions() in main.py AFTER fractional qty pre-computation and
BEFORE it is written to pending_decisions.json. The agents' prompts state
the investment rules, but prompt text is not a control — this gate is.

Rules enforced per decision:
  1. action ∈ {BUY, SELL, HOLD}            — anything else: REJECT
  2. ticker ∉ BLOCKED_TICKERS              — REJECT (defense in depth; the
     place_order hard block stays)
  3. ticker ∈ analyzed candidates ∪ holdings — unknown ticker: REJECT
     (an LLM must never trade a name no agent analyzed)
  4. same ticker BUY+SELL in one batch     — nonsensical PM output: REJECT both
  5. target_weight ∈ [0.0, MAX_TARGET_WEIGHT] — out of range: CLAMP and
     recompute qty (a 0.12 weight almost certainly means "max position";
     rejecting it would silently drop an intended trade)
  6. BUY notional ≤ MAX_BUY_NOTIONAL_PCT × total_value — REJECT, never clamp
     (a BUY that big after weight-clamping means the qty math went wrong).
     SELLs are exempt: a full exit of a position that has grown past the cap
     is exactly the de-risking trade this gate must not block; SELL qty is
     already bounded by available_qty in _compute_qty.
  7. notional ≥ MIN_ORDER_NOTIONAL         — below: SKIP (no-op, logged);
     kills sub-$5 broker rejections and churn trades
  8. Good-faith-violation guard (cash account): REJECT a SELL whose most
     recent broker-accepted BUY was within GFV_WINDOW_TRADING_DAYS trading
     days — unless the kill switch is active (risk exits always allowed).
     The system deliberately places SELLs before BUYs so same-day BUYs are
     routinely funded by unsettled proceeds; selling those positions the
     next day risks a GFV and, repeated, a 90-day account restriction.

HOLD decisions pass through untouched (no qty, no notional).

The result report is recorded to system_health.json under the
"decision_validation" check (DEGRADED when anything was rejected, clamped,
or skipped) so the existing alert.yml path surfaces every intervention.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from execute import BLOCKED_TICKERS, _compute_qty
from journal import _load_list, TRANSACTIONS_FILE

_ET = ZoneInfo("America/New_York")

VALID_ACTIONS            = {"BUY", "SELL", "HOLD"}
MAX_TARGET_WEIGHT        = 0.10
MAX_BUY_NOTIONAL_PCT     = 0.12
MIN_ORDER_NOTIONAL       = 5.00
GFV_WINDOW_TRADING_DAYS  = 2


def _trading_days_since(buy_date: str, today: str) -> int:
    """Count weekdays in (buy_date, today]. Buy Thu → sell Fri = 1;
    buy Thu → sell Mon = 2; buy Fri → sell Mon = 1.

    Weekday-aware only — a market holiday inside the window counts as a
    trading day, slightly relaxing the guard that week. Accepted: the
    2-day window already buffers T+1 settlement.
    """
    d   = datetime.strptime(buy_date, "%Y-%m-%d").date()
    end = datetime.strptime(today, "%Y-%m-%d").date()
    days = 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _last_live_buy_date(ticker: str, transactions: list) -> str | None:
    """Most recent broker-accepted (non-dry-run) BUY date for ticker.

    Runs at validation time, so the current run's own decisions are not in
    transactions.json yet — that is correct: a same-batch BUY+SELL of one
    ticker is rejected separately by rule 4, so there is nothing same-day
    to look up here. Do not "fix" this by including pending decisions.
    """
    dates = [
        tx.get("date") for tx in transactions
        if tx.get("ticker") == ticker
        and str(tx.get("action", "")).upper() == "BUY"
        and not tx.get("dry_run")
        and tx.get("date")
    ]
    return max(dates) if dates else None


def validate_decisions(
    decisions: list[dict],
    portfolio: dict,
    prices: dict,
    candidates: list[str],
    kill_active: bool = False,
    transactions: list | None = None,
) -> tuple[list[dict], dict]:
    """Validate LLM trade decisions. Returns (validated_decisions, report).

    `report` = {"passed": int, "rejected": [...], "modified": [...],
    "skipped": [...]} — each entry {"ticker", "action", "reason"}.
    Rejected and skipped decisions are removed from the returned list;
    modified (clamped) decisions are returned with corrected weight AND qty.
    """
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)

    today       = datetime.now(_ET).strftime("%Y-%m-%d")
    total_value = float(portfolio.get("total_value", 0) or 0)
    holdings    = {p.get("symbol") for p in portfolio.get("positions", [])}
    universe    = set(candidates) | holdings

    report: dict = {"passed": 0, "rejected": [], "modified": [], "skipped": []}

    def _reject(d, reason):
        report["rejected"].append(
            {"ticker": d.get("ticker", "?"), "action": d.get("action", "?"), "reason": reason})
        print(f"   🚫 VALIDATION REJECT: {d.get('action', '?')} {d.get('ticker', '?')} — {reason}")

    # Rule 4 pre-scan: same ticker on both sides of one batch
    sides: dict[str, set] = {}
    for d in decisions:
        a = str(d.get("action", "")).upper()
        if a in ("BUY", "SELL"):
            sides.setdefault(d.get("ticker", ""), set()).add(a)
    conflicted = {t for t, s in sides.items() if s == {"BUY", "SELL"}}

    validated: list[dict] = []
    for d in decisions:
        action = str(d.get("action", "")).upper()
        ticker = d.get("ticker", "")

        if action not in VALID_ACTIONS:
            _reject(d, f"invalid action {d.get('action')!r}")
            continue

        if action == "HOLD":           # nothing to validate — no order is placed
            validated.append(d)
            continue

        if not ticker:
            _reject(d, "missing ticker")
            continue

        if ticker in BLOCKED_TICKERS:
            _reject(d, "hard-blocked ticker")
            continue

        if ticker not in universe:
            _reject(d, "ticker not in analyzed candidates or current holdings")
            continue

        if ticker in conflicted:
            _reject(d, "same ticker appears as both BUY and SELL in one batch")
            continue

        try:
            weight = float(d.get("target_weight"))
        except (TypeError, ValueError):
            _reject(d, f"target_weight not a number: {d.get('target_weight')!r}")
            continue

        if not (0.0 <= weight <= MAX_TARGET_WEIGHT):
            clamped = min(max(weight, 0.0), MAX_TARGET_WEIGHT)
            # The pre-computed qty came from the out-of-range weight — it MUST
            # be recomputed or the clamp changes nothing at execution time.
            d = {**d, "target_weight": clamped,
                 "qty": _compute_qty(clamped, action, ticker, portfolio, prices)}
            report["modified"].append(
                {"ticker": ticker, "action": action,
                 "reason": f"target_weight {weight} clamped to {clamped}, qty recomputed"})
            print(f"   ⚠️  VALIDATION CLAMP: {action} {ticker} weight {weight} → {clamped}")

        if action == "SELL" and not kill_active:
            buy_date = _last_live_buy_date(ticker, transactions)
            if buy_date and _trading_days_since(buy_date, today) < GFV_WINDOW_TRADING_DAYS:
                _reject(d, f"good-faith-violation guard: bought {buy_date}, "
                           f"< {GFV_WINDOW_TRADING_DAYS} trading days ago (cash account)")
                continue

        qty      = float(d.get("qty") or 0)
        price    = float(prices.get(ticker, {}).get("close", 0) or 0)
        notional = qty * price

        if action == "BUY" and total_value > 0 and notional > MAX_BUY_NOTIONAL_PCT * total_value:
            _reject(d, f"BUY notional ${notional:.2f} exceeds "
                       f"{MAX_BUY_NOTIONAL_PCT:.0%} of portfolio (${total_value:.2f}) — qty math suspect")
            continue

        if 0 < notional < MIN_ORDER_NOTIONAL:
            report["skipped"].append(
                {"ticker": ticker, "action": action,
                 "reason": f"notional ${notional:.2f} below ${MIN_ORDER_NOTIONAL:.2f} minimum"})
            print(f"   ⏸  VALIDATION SKIP: {action} {ticker} notional ${notional:.2f} < ${MIN_ORDER_NOTIONAL:.2f}")
            continue

        validated.append(d)
        report["passed"] += 1

    return validated, report
