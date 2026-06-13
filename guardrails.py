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
MAX_SECTOR_WEIGHT        = 0.25   # hard cap on projected post-trade sector weight


# Static ticker → sector map for the current universe. The data layer carries
# no sector field (free-tier Polygon returns no fundamentals), so the 25%
# sector cap — previously enforced only in the PM prompt, i.e. not enforced —
# needs this map to become a code-level control.
#
# Reasonable GICS-style buckets; exactness is not required (a slightly-off
# bucket only shifts which marginal BUY is rejected, never loses capital — a
# rejected BUY forgoes a trade, it cannot lose money). Unknown tickers fall to
# "UNKNOWN" and share one conservative bucket, so an unmapped name still counts
# toward a cap rather than escaping it.
# TODO: source sectors from Polygon /v3/reference/tickers (sic_description /
# sector) when a paid tier is available, and fall back to this map.
SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "ADBE": "Technology", "AMAT": "Technology",
    "AMD": "Technology", "ARM": "Technology", "AVGO": "Technology",
    "CRM": "Technology", "CRWD": "Technology", "DDOG": "Technology",
    "IBM": "Technology", "INTC": "Technology", "MDB": "Technology",
    "MRVL": "Technology", "MSFT": "Technology", "MSTR": "Technology",
    "MU": "Technology", "NET": "Technology", "NOW": "Technology",
    "NVDA": "Technology", "ORCL": "Technology", "PANW": "Technology",
    "PLTR": "Technology", "QCOM": "Technology", "SMCI": "Technology",
    "SNOW": "Technology", "TEAM": "Technology", "TXN": "Technology",
    "WDAY": "Technology", "ZS": "Technology",
    # Communication Services
    "GOOG": "Communication Services", "GOOGL": "Communication Services",
    "META": "Communication Services", "NFLX": "Communication Services",
    "SPOT": "Communication Services",
    # Consumer Discretionary
    "ABNB": "Consumer Discretionary", "AMZN": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "CMG": "Consumer Discretionary",
    "EBAY": "Consumer Discretionary", "HD": "Consumer Discretionary",
    "LOW": "Consumer Discretionary", "LULU": "Consumer Discretionary",
    "MCD": "Consumer Discretionary", "NKE": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "TGT": "Consumer Discretionary",
    "TJX": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    # Consumer Staples
    "COST": "Consumer Staples", "WMT": "Consumer Staples",
    # Financials (incl. payment networks / fintech)
    "AXP": "Financials", "BAC": "Financials", "BLK": "Financials",
    "C": "Financials", "COIN": "Financials", "GS": "Financials",
    "JPM": "Financials", "MA": "Financials", "MS": "Financials",
    "PYPL": "Financials", "V": "Financials", "WFC": "Financials",
    # Health Care
    "ABBV": "Health Care", "AMGN": "Health Care", "BMY": "Health Care",
    "DHR": "Health Care", "GILD": "Health Care", "ISRG": "Health Care",
    "JNJ": "Health Care", "LLY": "Health Care", "MRK": "Health Care",
    "PFE": "Health Care", "REGN": "Health Care", "TMO": "Health Care",
    "UNH": "Health Care", "VRTX": "Health Care",
    # Industrials
    "BA": "Industrials", "CAT": "Industrials", "DE": "Industrials",
    "GE": "Industrials", "HON": "Industrials", "LMT": "Industrials",
    "RTX": "Industrials", "UBER": "Industrials", "UPS": "Industrials",
    # Energy
    "COP": "Energy", "CVX": "Energy", "EOG": "Energy", "OXY": "Energy",
    "SLB": "Energy", "XOM": "Energy",
    # Materials
    "FCX": "Materials", "LIN": "Materials", "NEM": "Materials",
    # Real Estate
    "AMT": "Real Estate", "EQIX": "Real Estate", "PLD": "Real Estate",
    # Utilities
    "NEE": "Utilities",
    # Benchmarks (never traded — excluded from candidates)
    "SPY": "ETF", "QQQ": "ETF",
}


def sector_of(ticker: str) -> str:
    return SECTOR_MAP.get(ticker, "UNKNOWN")


def enforce_sector_limits(
    decisions: list[dict],
    portfolio: dict,
    sectors: dict[str, str] | None = None,
    max_sector_weight: float = MAX_SECTOR_WEIGHT,
) -> tuple[list[dict], list[dict]]:
    """Reject BUYs that would push any sector over max_sector_weight.

    Returns (kept, rejected). Rejected entries are the original decision dicts
    annotated with a `rejected_reason`. The projected post-trade weight of a
    traded name is its `target_weight` (the PM's target is an absolute weight,
    not an increment); untouched holdings keep their current weight. SELLs are
    applied first so a same-sector exit frees budget for a later BUY — even when
    decisions arrive BUY-first. Decision order is otherwise preserved.

    Runs AFTER validate_decisions (so same-ticker BUY+SELL conflicts and
    weight-clamping are already resolved) and is recorded under the same
    `decision_validation` health check in main.py.
    """
    if sectors is None:
        sectors = SECTOR_MAP
    sec_of = lambda t: sectors.get(t, "UNKNOWN")

    total = float(portfolio.get("total_value", 0) or 0)
    # Projected per-ticker weight, seeded from current holdings.
    proj: dict[str, float] = {}
    for p in portfolio.get("positions", []):
        sym = p.get("symbol")
        if sym:
            proj[sym] = (float(p.get("market_value", 0) or 0) / total) if total else 0.0

    # Pass 1: apply SELLs up front to free sector budget (target_weight is the
    # weight the position is reduced TO — 0.0 for a full exit).
    for d in decisions:
        if str(d.get("action", "")).upper() == "SELL":
            proj[d.get("ticker", "")] = float(d.get("target_weight", 0) or 0)

    def sector_weight(sec: str, exclude: str) -> float:
        return sum(w for t, w in proj.items()
                   if sec_of(t) == sec and t != exclude)

    kept, rejected = [], []
    # Pass 2: evaluate BUYs in original order; accepted BUYs accrue into proj
    # so a second BUY in the same sector sees the first one's weight.
    for d in decisions:
        action = str(d.get("action", "")).upper()
        if action != "BUY":
            kept.append(d)   # SELL / HOLD: never blocked by the sector cap
            continue
        ticker    = d.get("ticker", "")
        tw        = float(d.get("target_weight", 0) or 0)
        sec       = sec_of(ticker)
        projected = sector_weight(sec, exclude=ticker) + tw
        if projected > max_sector_weight + 1e-9:
            reason = (f"{sec} sector would be {projected:.0%} > "
                      f"{max_sector_weight:.0%} cap")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 SECTOR REJECT: BUY {ticker} — {reason}")
            continue
        proj[ticker] = tw
        kept.append(d)

    return kept, rejected


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
