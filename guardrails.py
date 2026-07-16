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
from policy import VALUES as _POLICY

_ET = ZoneInfo("America/New_York")

# Operative limits are now SINGLE-SOURCED from policy.yaml (mirror of IPS.md
# Appendix A) via policy.py. The names below are unchanged so every caller and
# test still imports `from guardrails import MAX_SECTOR_WEIGHT` etc. The loader
# falls back to the historical constants if policy.yaml is unreadable, so this is
# a zero-behavior-change refactor (asserted by TestPolicyParity).
VALID_ACTIONS            = {"BUY", "SELL", "HOLD"}   # not a tunable limit
MAX_TARGET_WEIGHT        = _POLICY["max_target_weight"]
MAX_BUY_NOTIONAL_PCT     = _POLICY["max_buy_notional_pct"]
MIN_ORDER_NOTIONAL       = _POLICY["min_order_notional"]
GFV_WINDOW_TRADING_DAYS  = _POLICY["gfv_window_trading_days"]
MAX_SECTOR_WEIGHT        = _POLICY["max_sector_weight"]   # hard cap on projected post-trade sector weight

# Turnover / tax discipline (this is a CALIFORNIA TOP-BRACKET TAXABLE account).
# Every sale realizes a SHORT-TERM gain taxed at ~54% (37% fed + 3.8% NIIT +
# 13.3% CA), so the dominant after-tax lever is simply trading LESS. These two
# controls are deliberately stronger than the GFV guard above: they exist to cut
# the documented weekly momentum-rotation churn, not to manage settlement.
MIN_HOLDING_TRADING_DAYS = _POLICY["min_holding_trading_days"]  # don't SELL a name bought < N trading days ago (risk exits exempt) — 30 since policy v2.0 (IPS §7.2)
WASH_SALE_REENTRY_DAYS   = _POLICY["wash_sale_reentry_days"]    # don't BUY a name SOLD within N calendar days (wash-sale + anti-churn)
MIN_NET_EDGE             = _POLICY["min_net_edge"]              # $ floor: a BUY's expected edge must clear cost + CA ST tax (#6; tunable)
TAX_AWARE_HOLD_WINDOW    = _POLICY["tax_aware_hold_window_trading_days"]  # block discretionary SELL of a gained lot near its 1-year LT date (IPS §7.5)
SAFE_MODE_INDEX_DROP_PCT = _POLICY["safe_mode_index_drop_pct"]  # PERCENT — SPY 1-day drop ≥ this halts all new BUYs (§18.5 crisis safe-mode)


# Static ticker → sector map for the current universe. The data layer carries
# no sector field (free-tier Polygon returns no fundamentals), so the 25%
# sector cap — previously enforced only in the PM prompt, i.e. not enforced —
# needs this map to become a code-level control.
#
# Reasonable GICS-style buckets; exactness is not required (a slightly-off
# bucket only shifts which marginal BUY is rejected, never loses capital — a
# rejected BUY forgoes a trade, it cannot lose money). Unmapped tickers fall to
# "UNKNOWN", and a BUY of an UNKNOWN-sector name is REJECTED outright
# (fail-closed in enforce_sector_limits): the Jul 8 2026 rebalance proved the
# old "UNKNOWN shares one bucket" design escapes the cap rather than enforcing
# it — CB+CFG (unmapped financials) split the true Financials exposure across
# two under-cap buckets and a 35%-financials book passed a 25% cap. The map
# MUST cover universe.EXPANDED_UNIVERSE in full (TestSectorMapCompleteness
# enforces this structurally).
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

    # ── Expansion universe (Stage D, universe._EXPANSION) — GICS sectors ─────
    # Post-2023-GICS-restructure assignments (payment processors → Financials;
    # DG/DLTR → Consumer Staples; ADP/PAYX → Industrials Professional Services).
    # Technology
    "ACN": "Technology", "ADI": "Technology", "ADSK": "Technology",
    "AKAM": "Technology", "ANET": "Technology", "ANSS": "Technology",
    "APH": "Technology", "CDNS": "Technology", "CDW": "Technology",
    "CSCO": "Technology", "DELL": "Technology", "DOCU": "Technology",
    "ENPH": "Technology", "FICO": "Technology", "FSLR": "Technology",
    "FTNT": "Technology", "GLW": "Technology", "GRMN": "Technology",
    "HPE": "Technology", "HPQ": "Technology", "HUBS": "Technology",
    "INTU": "Technology", "IT": "Technology", "JNPR": "Technology",
    "KEYS": "Technology", "KLAC": "Technology", "LRCX": "Technology",
    "MCHP": "Technology", "MPWR": "Technology", "MSI": "Technology",
    "NXPI": "Technology", "OKTA": "Technology", "ON": "Technology",
    "PTC": "Technology", "ROP": "Technology", "SNPS": "Technology",
    "STX": "Technology", "SWKS": "Technology", "TDY": "Technology",
    "TER": "Technology", "TTD": "Technology", "TWLO": "Technology",
    "TYL": "Technology", "WDC": "Technology", "ZBRA": "Technology",
    "ZM": "Technology",
    # Communication Services
    "CHTR": "Communication Services", "CMCSA": "Communication Services",
    "DIS": "Communication Services", "EA": "Communication Services",
    "FOXA": "Communication Services", "IPG": "Communication Services",
    "OMC": "Communication Services", "PARA": "Communication Services",
    "PINS": "Communication Services", "RBLX": "Communication Services",
    "ROKU": "Communication Services", "SNAP": "Communication Services",
    "T": "Communication Services", "TMUS": "Communication Services",
    "TTWO": "Communication Services", "VZ": "Communication Services",
    "WBD": "Communication Services",
    # Financials (incl. exchanges, insurers, payment processing per GICS 2023)
    "AFL": "Financials", "AIG": "Financials", "AJG": "Financials",
    "ALL": "Financials", "AMP": "Financials", "AON": "Financials",
    "BK": "Financials", "CB": "Financials", "CFG": "Financials",
    "CME": "Financials", "COF": "Financials", "DFS": "Financials",
    "FIS": "Financials", "FISV": "Financials", "FITB": "Financials",
    "GPN": "Financials", "HBAN": "Financials", "ICE": "Financials",
    "KEY": "Financials", "MCO": "Financials", "MET": "Financials",
    "MMC": "Financials", "MSCI": "Financials", "MTB": "Financials",
    "NDAQ": "Financials", "NTRS": "Financials", "PGR": "Financials",
    "PNC": "Financials", "PRU": "Financials", "RF": "Financials",
    "SCHW": "Financials", "SPGI": "Financials", "STT": "Financials",
    "SYF": "Financials", "TFC": "Financials", "TROW": "Financials",
    "TRV": "Financials", "USB": "Financials",
    # Health Care
    "A": "Health Care", "ABT": "Health Care", "ALGN": "Health Care",
    "BAX": "Health Care", "BDX": "Health Care", "BIIB": "Health Care",
    "BSX": "Health Care", "CAH": "Health Care", "CI": "Health Care",
    "CNC": "Health Care", "COR": "Health Care", "CVS": "Health Care",
    "DXCM": "Health Care", "ELV": "Health Care", "EW": "Health Care",
    "GEHC": "Health Care", "HCA": "Health Care", "HOLX": "Health Care",
    "HUM": "Health Care", "IDXX": "Health Care", "IQV": "Health Care",
    "MCK": "Health Care", "MDT": "Health Care", "MRNA": "Health Care",
    "MTD": "Health Care", "PODD": "Health Care", "RMD": "Health Care",
    "STE": "Health Care", "SYK": "Health Care", "WST": "Health Care",
    "ZBH": "Health Care", "ZTS": "Health Care",
    # Consumer Staples (incl. DG/DLTR/WBA per GICS 2023 Staples Distribution)
    "ADM": "Consumer Staples", "CAG": "Consumer Staples",
    "CHD": "Consumer Staples", "CL": "Consumer Staples",
    "CLX": "Consumer Staples", "CPB": "Consumer Staples",
    "DG": "Consumer Staples", "DLTR": "Consumer Staples",
    "GIS": "Consumer Staples", "HRL": "Consumer Staples",
    "HSY": "Consumer Staples", "K": "Consumer Staples",
    "KDP": "Consumer Staples", "KHC": "Consumer Staples",
    "KMB": "Consumer Staples", "KO": "Consumer Staples",
    "KR": "Consumer Staples", "MDLZ": "Consumer Staples",
    "MKC": "Consumer Staples", "MNST": "Consumer Staples",
    "MO": "Consumer Staples", "PEP": "Consumer Staples",
    "PG": "Consumer Staples", "PM": "Consumer Staples",
    "STZ": "Consumer Staples", "SYY": "Consumer Staples",
    "TSN": "Consumer Staples", "WBA": "Consumer Staples",
    # Consumer Discretionary
    "APTV": "Consumer Discretionary", "AZO": "Consumer Discretionary",
    "BBY": "Consumer Discretionary", "BWA": "Consumer Discretionary",
    "CCL": "Consumer Discretionary", "DASH": "Consumer Discretionary",
    "DHI": "Consumer Discretionary", "DRI": "Consumer Discretionary",
    "EXPE": "Consumer Discretionary", "F": "Consumer Discretionary",
    "GM": "Consumer Discretionary", "GPC": "Consumer Discretionary",
    "HLT": "Consumer Discretionary", "LEN": "Consumer Discretionary",
    "LVS": "Consumer Discretionary", "MAR": "Consumer Discretionary",
    "MGM": "Consumer Discretionary", "NCLH": "Consumer Discretionary",
    "NVR": "Consumer Discretionary", "ORLY": "Consumer Discretionary",
    "PHM": "Consumer Discretionary", "POOL": "Consumer Discretionary",
    "RCL": "Consumer Discretionary", "ROST": "Consumer Discretionary",
    "TSCO": "Consumer Discretionary", "ULTA": "Consumer Discretionary",
    "WYNN": "Consumer Discretionary", "YUM": "Consumer Discretionary",
    # Industrials (incl. ADP/PAYX/VRSK/EFX Professional Services per GICS 2023)
    "ADP": "Industrials", "AME": "Industrials", "CARR": "Industrials",
    "CMI": "Industrials", "CSX": "Industrials", "CTAS": "Industrials",
    "DOV": "Industrials", "EFX": "Industrials", "EMR": "Industrials",
    "ETN": "Industrials", "FAST": "Industrials", "FDX": "Industrials",
    "FTV": "Industrials", "GD": "Industrials", "IR": "Industrials",
    "ITW": "Industrials", "JCI": "Industrials", "LHX": "Industrials",
    "MMM": "Industrials", "NOC": "Industrials", "NSC": "Industrials",
    "ODFL": "Industrials", "OTIS": "Industrials", "PAYX": "Industrials",
    "PCAR": "Industrials", "PH": "Industrials", "PWR": "Industrials",
    "ROK": "Industrials", "RSG": "Industrials", "SWK": "Industrials",
    "TDG": "Industrials", "UNP": "Industrials", "URI": "Industrials",
    "VRSK": "Industrials", "WAB": "Industrials", "WM": "Industrials",
    "XYL": "Industrials",
    # Energy
    "BKR": "Energy", "DVN": "Energy", "FANG": "Energy", "HAL": "Energy",
    "HES": "Energy", "KMI": "Energy", "MPC": "Energy", "OKE": "Energy",
    "PSX": "Energy", "PXD": "Energy", "VLO": "Energy", "WMB": "Energy",
    # Utilities
    "AEE": "Utilities", "AEP": "Utilities", "CNP": "Utilities",
    "D": "Utilities", "DTE": "Utilities", "DUK": "Utilities",
    "ED": "Utilities", "EIX": "Utilities", "ES": "Utilities",
    "EXC": "Utilities", "FE": "Utilities", "PCG": "Utilities",
    "PEG": "Utilities", "PPL": "Utilities", "SO": "Utilities",
    "SRE": "Utilities", "WEC": "Utilities", "XEL": "Utilities",
    # Materials
    "ALB": "Materials", "APD": "Materials", "AVY": "Materials",
    "BALL": "Materials", "CE": "Materials", "CF": "Materials",
    "CTVA": "Materials", "DD": "Materials", "DOW": "Materials",
    "ECL": "Materials", "IFF": "Materials", "IP": "Materials",
    "MLM": "Materials", "MOS": "Materials", "NUE": "Materials",
    "PKG": "Materials", "PPG": "Materials", "SHW": "Materials",
    "STLD": "Materials", "VMC": "Materials",
    # Real Estate
    "ARE": "Real Estate", "AVB": "Real Estate", "CBRE": "Real Estate",
    "CCI": "Real Estate", "DLR": "Real Estate", "EQR": "Real Estate",
    "EXR": "Real Estate", "INVH": "Real Estate", "IRM": "Real Estate",
    "MAA": "Real Estate", "O": "Real Estate", "PSA": "Real Estate",
    "SBAC": "Real Estate", "SPG": "Real Estate", "VICI": "Real Estate",
    "VTR": "Real Estate", "WELL": "Real Estate",
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

    FAIL-CLOSED on unmapped tickers: a BUY whose sector resolves to "UNKNOWN"
    is rejected outright — its concentration cannot be risk-checked, and the
    Jul 8 2026 incident showed an UNKNOWN bucket lets the TRUE sector escape
    the cap (CB+CFG passed at a realized 35% financials vs the 25% cap).
    SELLs and HOLDs always pass (an exit must never be blocked by a map gap).

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
        if sec == "UNKNOWN":
            reason = ("sector unmapped — fail-closed (cannot risk-check "
                      "concentration; add the ticker to SECTOR_MAP)")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 SECTOR REJECT: BUY {ticker} — {reason}")
            continue
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


def _last_live_trade_date(ticker: str, transactions: list, side: str) -> str | None:
    """Most recent broker-accepted (non-dry-run) `side` ('BUY'|'SELL') date for ticker.

    Single source of truth for "what counts as a real prior trade" — both the
    holding-period and wash-sale guards depend on it, so they can never disagree
    on what a live fill is.
    """
    side = side.upper()
    dates = [
        tx.get("date") for tx in transactions
        if tx.get("ticker") == ticker
        and str(tx.get("action", "")).upper() == side
        and not tx.get("dry_run")
        and tx.get("date")
    ]
    return max(dates) if dates else None


def _last_live_buy_date(ticker: str, transactions: list) -> str | None:
    """Most recent broker-accepted BUY date for ticker.

    Runs at validation time, so the current run's own decisions are not in
    transactions.json yet — that is correct: a same-batch BUY+SELL of one
    ticker is rejected separately by rule 4, so there is nothing same-day
    to look up here. Do not "fix" this by including pending decisions.
    """
    return _last_live_trade_date(ticker, transactions, "BUY")


def _last_live_sell_date(ticker: str, transactions: list) -> str | None:
    """Most recent broker-accepted SELL date for ticker (wash-sale/anti-churn guard)."""
    return _last_live_trade_date(ticker, transactions, "SELL")


def enforce_min_holding_period(
    decisions: list[dict],
    portfolio: dict,
    transactions: list | None = None,
    kill_active: bool = False,
    min_holding_days: int = MIN_HOLDING_TRADING_DAYS,
    today: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Reject discretionary SELLs of names bought < min_holding_days trading days ago.

    Anti-churn / tax control. In a CA top-bracket taxable account every sale is a
    short-term gain (~54%), so cutting round-trip frequency is the dominant
    after-tax lever. Returns (kept, rejected); rejected entries carry a
    `rejected_reason` and are folded into the decision_validation health check.

    Exemptions / safe defaults:
      - kill_active → return everything untouched (risk exits must never be blocked).
      - A SELL whose ticker has NO in-log BUY date is NOT blocked. A position
        opened before transaction logging (most current holdings) can't be proven
        recent, and a long-held exit is exactly the trade we must allow.
      - BUY / HOLD pass through.

    Note: a rejected SELL also drops any same-batch BUY that named it as
    source_of_capital — see enforce_capital_dependency. (The pre-Jul-2026
    assumption that such a BUY would simply "fail at the broker for lack of
    cash" was false on a high-cash book: on Jul 8 2026 the CB/CFG BUYs filled
    from cash after their funding SELLs were rejected, turning a CRO-approved
    rotation into an unreviewed net addition that breached the sector cap.)
    """
    if kill_active:
        return list(decisions), []     # risk regime: never block exits (skip the file read)
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)

    today = today or datetime.now(_ET).strftime("%Y-%m-%d")
    kept, rejected = [], []
    for d in decisions:
        if str(d.get("action", "")).upper() != "SELL":
            kept.append(d)
            continue
        ticker   = d.get("ticker", "")
        buy_date = _last_live_buy_date(ticker, transactions)
        if buy_date and _trading_days_since(buy_date, today) < min_holding_days:
            reason = (f"min-holding: bought {buy_date}, < {min_holding_days} "
                      f"trading days ago (anti-churn/tax)")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 HOLDING REJECT: SELL {ticker} — {reason}")
        else:
            kept.append(d)
    return kept, rejected


def enforce_capital_dependency(
    decisions: list[dict],
    rejected: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Drop any BUY whose funding SELL was rejected by an earlier guard.

    A PM rotation (SELL X → BUY Y with source_of_capital=X) is one decision,
    approved by the CRO as a pair against a projected post-rotation book. If
    the SELL leg is rejected (min-holding, tax-aware hold, validation), the BUY
    leg must not execute alone: on Jul 8 2026 the orphaned CB/CFG BUYs filled
    from cash after their AXP/MS funding SELLs were min-hold-rejected — the
    realized book (financials-heavy, cash halved) was one the CRO never
    reviewed. A rotation executes whole or not at all.

    Pure function. `decisions` is the post-guard kept list; `rejected` is every
    decision rejected so far this run. A BUY is dropped iff its
    source_of_capital (case-insensitive) equals the ticker of a rejected SELL.
    source_of_capital of "cash"/empty/None is exempt (no dependency). SELLs and
    HOLDs always pass. Failure direction: a dropped BUY is a missed trade —
    the safe direction — and the PM may re-propose it next rebalance.
    """
    rejected_sells = {
        str(r.get("ticker", "")).upper():
            r.get("rejected_reason") or r.get("reason") or "rejected"
        for r in rejected
        if str(r.get("action", "")).upper() == "SELL" and r.get("ticker")
    }
    if not rejected_sells:
        return list(decisions), []

    kept, newly_rejected = [], []
    for d in decisions:
        src = str(d.get("source_of_capital") or "").upper()
        if (str(d.get("action", "")).upper() == "BUY"
                and src in rejected_sells):
            reason = (f"funding SELL {src} was rejected "
                      f"({rejected_sells[src]}) — rotation executes whole or "
                      f"not at all")
            newly_rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 DEPENDENCY REJECT: BUY {d.get('ticker','')} — {reason}")
        else:
            kept.append(d)
    return kept, newly_rejected


def min_hold_days_remaining(
    ticker: str,
    transactions: list | None = None,
    today: str | None = None,
    min_holding_days: int = MIN_HOLDING_TRADING_DAYS,
) -> int | None:
    """Trading days until `ticker` clears the min-holding guard, or None.

    None means "no live BUY on record" — the position predates transaction
    logging and is freely sellable (mirrors enforce_min_holding_period's
    no-buy-date exemption). 0 means sellable now. Used to show the PM which
    holdings are actually eligible for a discretionary SELL, so it stops
    proposing rotations the guard is guaranteed to reject (Jul 8 2026: both
    SELL legs rejected, DEGRADED health, orphaned BUYs).
    """
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)
    buy_date = _last_live_buy_date(ticker, transactions)
    if not buy_date:
        return None
    today = today or datetime.now(_ET).strftime("%Y-%m-%d")
    return max(0, min_holding_days - _trading_days_since(buy_date, today))


def wash_sale_days_remaining(
    ticker: str,
    transactions: list | None = None,
    today: str | None = None,
    window_days: int = WASH_SALE_REENTRY_DAYS,
) -> int | None:
    """Calendar days until `ticker` clears the wash-sale re-entry guard, or None.

    BUY-side mirror of min_hold_days_remaining, using the SAME date arithmetic as
    enforce_wash_sale_reentry (calendar days since the last live SELL). None means
    "no live SELL on record" — freely buyable. 0 means buyable now. Used to show
    the PM which candidates are actually BUY-eligible, so it stops proposing
    re-entries the guard is guaranteed to reject (Jun 25–Jul 2 2026: JNJ/TJX/V
    BUYs proposed repeatedly, silently rejected, cash stuck at 46%+ — the PM's
    recently_exited warning covered only 10 of the 30 blocked days).
    """
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)
    sell_date = _last_live_sell_date(ticker, transactions)
    if not sell_date:
        return None
    today_date = (datetime.strptime(today, "%Y-%m-%d").date()
                  if today else datetime.now(_ET).date())
    try:
        days = (today_date - datetime.strptime(sell_date, "%Y-%m-%d").date()).days
    except ValueError:
        return None
    if days < 0:  # future-dated SELL row — malformed; fail open like the guard
        return None
    return max(0, window_days - days)


def enforce_wash_sale_reentry(
    decisions: list[dict],
    transactions: list | None = None,
    window_days: int = WASH_SALE_REENTRY_DAYS,
    today: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Reject BUYs of names SOLD within `window_days` calendar days.

    Hardens and widens the soft `recently_exited` PM warning (10d, prompt-only)
    into a code control (30d, enforced). Re-buying a just-sold name is the churn
    pattern, and within 30 calendar days a *loss* sale triggers the IRS wash-sale
    rule (disallowing the harvested loss). This block is intentionally broader
    than the IRS rule — it also blocks gain re-entries — because the goal here is
    to cut turnover in a high-churn taxable account. Returns (kept, rejected).
    """
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)
    today_date = (datetime.strptime(today, "%Y-%m-%d").date()
                  if today else datetime.now(_ET).date())

    kept, rejected = [], []
    for d in decisions:
        if str(d.get("action", "")).upper() != "BUY":
            kept.append(d)
            continue
        ticker    = d.get("ticker", "")
        sell_date = _last_live_sell_date(ticker, transactions)
        days = None
        if sell_date:
            try:
                days = (today_date - datetime.strptime(sell_date, "%Y-%m-%d").date()).days
            except ValueError:
                days = None
        if days is not None and 0 <= days < window_days:
            reason = (f"wash-sale/anti-churn: sold {sell_date}, {days}d ago "
                      f"(< {window_days}d re-entry block)")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 REENTRY REJECT: BUY {ticker} — {reason}")
        else:
            kept.append(d)
    return kept, rejected


def enforce_tax_aware_hold(
    decisions: list[dict],
    prices: dict,
    transactions: list | None = None,
    kill_active: bool = False,
    window_trading_days: int = TAX_AWARE_HOLD_WINDOW,
    today: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Reject a DISCRETIONARY SELL of a position holding a GAINED lot within
    ~`window_trading_days` of its 1-year long-term-tax date (IPS §7.5).

    The 9–12mo horizon sits right on the short-term (~54%) / long-term (~37%)
    boundary: a winner sold at month 11 pays ~54% tax; held three more weeks past
    one year, ~37% — nearly HALF the tax on the same gain. When a gained lot is
    that close to crossing, the after-tax expected value of waiting almost always
    beats a discretionary trim, so the trim is blocked and re-proposed post-boundary.

    Per-lot FIFO dates via tax_lots.open_lots (P0-4 — multiple lots have multiple
    1-year dates; any qualifying lot blocks, since a FIFO sale consumes the oldest
    lot first, which is exactly the near-boundary one).

    Exemptions (risk exits always outrank tax timing):
      - kill_active → untouched;
      - a decision carrying risk_exit=True (risk_watch stop-loss) → untouched;
      - lots at a LOSS → not blocked (harvesting a short-term loss is favorable);
      - lots already past 1 year → not blocked (already long-term).

    The window is TRADING days in policy; lots measure CALENDAR days — converted
    at 7/5 (≈42 calendar days). Returns (kept, rejected) like the other guards.
    """
    if kill_active:
        return list(decisions), []
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)
    today = today or datetime.now(_ET).strftime("%Y-%m-%d")
    window_calendar_days = round(window_trading_days * 7 / 5)
    from tax_lots import open_lots, holding_days

    kept, rejected = [], []
    for d in decisions:
        if str(d.get("action", "")).upper() != "SELL" or d.get("risk_exit"):
            kept.append(d)
            continue
        ticker  = d.get("ticker", "")
        sell_px = (prices.get(ticker) or {}).get("close")
        near_boundary = None
        if sell_px:
            for lot in open_lots(transactions, ticker):
                held  = holding_days(lot.get("acquired"), today)
                basis = lot.get("cost_basis", 0) or 0
                if (held is not None and basis
                        and float(sell_px) > basis                       # lot in GAIN
                        and 365 - window_calendar_days <= held < 365):   # near the LT boundary
                    near_boundary = {**lot, "held_days": held,
                                     "days_to_long_term": 365 - held}
                    break
        if near_boundary:
            reason = (f"tax-aware hold: gained lot acquired {near_boundary.get('acquired')} "
                      f"is {near_boundary['days_to_long_term']}d from its 1-year "
                      f"long-term date (~54% ST vs ~37% LT on the same gain) — "
                      f"discretionary SELL deferred past the boundary (IPS §7.5)")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 TAX-HOLD REJECT: SELL {ticker} — {reason}")
        else:
            kept.append(d)
    return kept, rejected


def crisis_safe_mode_active(
    spy_change_pct: float | None,
    threshold_pct: float = SAFE_MODE_INDEX_DROP_PCT,
) -> tuple[bool, str]:
    """(active, reason) — is the §18.5 market-wide crisis safe-mode tripped?

    Pure function on the SPY 1-day move (percent, negative = down). Per-name stops
    don't cover a market-wide event; on an index drop ≥ threshold the system must
    halt ALL new BUYs (risk-driven SELLs stay allowed) and alert the owner. None
    (no SPY data) → NOT active: the safe-mode is an extra brake on a known crash,
    never a data-outage trap that silently disables buying forever.
    """
    if not isinstance(spy_change_pct, (int, float)):
        return False, ""
    if spy_change_pct <= -abs(threshold_pct):
        return True, (f"SPY {spy_change_pct:+.1f}% breaches the -{abs(threshold_pct):.0f}% "
                      f"crisis safe-mode threshold — all new BUYs halted (§18.5)")
    return False, ""


def enforce_safe_mode(
    decisions: list[dict],
    spy_change_pct: float | None,
    threshold_pct: float = SAFE_MODE_INDEX_DROP_PCT,
) -> tuple[list[dict], list[dict], str]:
    """Drop every BUY when crisis safe-mode is active; SELLs/HOLDs pass untouched.

    Returns (kept, rejected, reason). reason is '' when safe-mode is not active."""
    active, reason = crisis_safe_mode_active(spy_change_pct, threshold_pct)
    if not active:
        return list(decisions), [], ""
    kept, rejected = [], []
    for d in decisions:
        if str(d.get("action", "")).upper() == "BUY":
            rejected.append({**d, "rejected_reason": f"crisis safe-mode: {reason}"})
            print(f"   🚨 SAFE-MODE REJECT: BUY {d.get('ticker', '?')} — {reason}")
        else:
            kept.append(d)
    return kept, rejected, reason


def flag_wash_sale_presale(
    decisions: list[dict],
    prices: dict,
    transactions: list | None = None,
    window_days: int = WASH_SALE_REENTRY_DAYS,
    today: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """FLAG (never block) loss SELLs within `window_days` of a purchase — the
    PRE-sale side of IRS §1091.

    enforce_wash_sale_reentry covers the POST-sale side (blocking a re-buy of a
    just-sold name). This covers the pre-sale side: selling a recently-bought lot
    at a loss. Together they bracket the full ±30-day wash-sale window.

    Policy is FLAG-AND-ALLOW by design (goal: maximize after-tax return). A wash
    sale does not destroy the loss — it DEFERS it onto the replacement lot's cost
    basis, so the only cost is timing. Blocking a risk- or conviction-driven exit
    to preserve that small timing benefit would expose real capital to further
    loss — exactly backwards. The flag (a) lets tax accounting defer the loss
    rather than double-count it as harvested, and (b) makes the event auditable.

    Uses tax_lots.open_lots (read-only FIFO over transactions), so it sees the
    pre-run lot state — the current run's own SELL is correctly not yet present.
    Returns (decisions, flagged); `decisions` length is unchanged. Matched SELLs
    gain a `wash_sale_presale` annotation.
    """
    if transactions is None:
        transactions = _load_list(TRANSACTIONS_FILE)
    today = today or datetime.now(_ET).strftime("%Y-%m-%d")
    from tax_lots import open_lots, holding_days

    flagged: list[dict] = []
    for d in decisions:
        if str(d.get("action", "")).upper() != "SELL":
            continue
        ticker  = d.get("ticker", "")
        sell_px = (prices.get(ticker) or {}).get("close")
        if not sell_px:
            continue
        recent_loss_lots = []
        for lot in open_lots(transactions, ticker):
            held = holding_days(lot.get("acquired"), today)
            basis = lot.get("cost_basis", 0) or 0
            if held is not None and 0 <= held < window_days and float(sell_px) < basis:
                recent_loss_lots.append({**lot, "held_days": held})
        if recent_loss_lots:
            info = {
                "window_days": window_days,
                "sell_price":  float(sell_px),
                "lots":        recent_loss_lots,
                "note": (f"loss exit within {window_days}d of purchase — IRS §1091 "
                         "wash sale: the loss is DEFERRED onto the replacement "
                         "basis, not lost. ALLOWED (capital-risk exit outranks tax "
                         "timing); flagged for accounting/audit."),
            }
            d["wash_sale_presale"] = info
            flagged.append({"ticker": ticker, **info})
            print(f"   ⚠ WASH-SALE FLAG (allowed): SELL {ticker} — "
                  f"loss within {window_days}d of purchase")
    return decisions, flagged


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


def enforce_net_edge(
    decisions: list[dict],
    prices: dict,
    min_net_edge: float = MIN_NET_EDGE,
) -> tuple[list[dict], list[dict]]:
    """Reject BUYs whose expected NET edge < min_net_edge after round-trip cost +
    CA short-term tax (cost_model.net_edge).

    Conditional on an explicit `expected_return` (the PM's gross-return estimate,
    a fraction): a decision without one is NOT evaluated — pass it through, so
    there is no regression before the PM reliably emits the field. SELLs are
    exempt: exits / de-risking must never be blocked by an edge floor. Returns
    (kept, rejected); rejected entries carry a `rejected_reason` (folded into the
    decision_validation health check in main.py).

    Why it matters: in a CA top-bracket taxable account a short-term gain is taxed
    ~54%, so a marginal BUY whose expected edge barely clears the gross hurdle is
    net-negative after tax + friction. This gate makes "is it worth it after tax?"
    a code-level control rather than a hope.
    """
    from cost_model import net_edge
    kept, rejected = [], []
    for d in decisions:
        action = str(d.get("action", "")).upper()
        try:
            er = float(d.get("expected_return"))
        except (TypeError, ValueError):
            er = 0.0
        if action != "BUY" or er <= 0:
            kept.append(d)
            continue
        ticker   = d.get("ticker", "")
        qty      = float(d.get("qty") or 0)
        price    = float((prices.get(ticker) or {}).get("close", 0) or 0)
        notional = qty * price
        if notional <= 0:
            kept.append(d)
            continue
        ne = net_edge(er, notional, short_term=True)
        if ne["net"] < min_net_edge:
            reason = (f"net edge ${ne['net']:.2f} < ${min_net_edge:.2f} floor after "
                      f"cost ${ne['cost']:.2f} + CA ST tax ${ne['tax']:.2f} "
                      f"(gross ${ne['gross']:.2f} on {er:.1%} expected return)")
            rejected.append({**d, "rejected_reason": reason})
            print(f"   🚫 NET-EDGE REJECT: BUY {ticker} — {reason}")
        else:
            kept.append(d)
    return kept, rejected
