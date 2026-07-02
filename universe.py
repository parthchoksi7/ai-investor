"""
universe.py — the canonical trading/scoring universe + gated expansion + fetch cursor.

Phase 2. Two things live here so there is ONE source of truth for "which names the
system looks at", and so the expansion is safe:

  1. **CORE_UNIVERSE** — the ~100 hand-picked liquid large-caps the system has always
     traded. Always active. (Historically `market_data.WATCHLIST`; moved here so the
     gate + cursor can reason about the universe without importing market_data.)

  2. **EXPANDED_UNIVERSE** — CORE plus a broad set of additional S&P-500-class names,
     targeting ~400 total (IPS §5). Expansion is **GATED**: the wider pool is admitted
     to scoring/trading ONLY when (a) the operator has enabled it AND (b) fundamental
     coverage has cleared the IPS 80% floor. A wider universe on <80% coverage just
     adds momentum-only names with no quality/value signal — the opposite of the
     Phase 2 goal — so `get_active_universe` keeps the core until both hold.

  3. **Resumable fetch cursor (`fetch_progress.json`)** — Polygon free tier is 5
     calls/min, so fetching 400×210-day histories in one run is impossible. `next_batch`
     hands out a bounded slice and persists a wrap-around cursor, so the full universe
     is covered over ceil(N / batch) runs and a crash resumes where it left off rather
     than restarting from ticker 0.

Bad/delisted tickers in the expansion degrade gracefully: `corporate_actions`
flags them and coverage measurement excludes them — they never corrupt a score.
"""

from __future__ import annotations

import json
import os

# ── CORE (always active) ──────────────────────────────────────────────────────
# The historical WATCHLIST, verbatim. market_data.WATCHLIST aliases this.
CORE_UNIVERSE: list[str] = [
    # Mega-cap Tech / AI / Cloud
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "ORCL", "IBM", "INTC", "QCOM", "TXN", "MU", "AMAT",
    # Software / SaaS
    "CRM", "ADBE", "NOW", "SNOW", "DDOG", "ZS", "CRWD", "PANW",
    "TEAM", "WDAY", "MDB", "NET",
    # Semiconductors
    "AMD", "AVGO", "ARM", "MRVL", "SMCI",
    # Consumer Tech / Internet
    "NFLX", "SPOT", "UBER", "ABNB", "BKNG", "EBAY",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA", "PYPL",
    # Healthcare / Biotech / Pharma
    "JNJ", "UNH", "LLY", "ABBV", "PFE", "MRK", "BMY", "GILD", "AMGN",
    "REGN", "VRTX", "ISRG", "TMO", "DHR",
    # Consumer Discretionary / Retail
    "HD", "LOW", "TGT", "WMT", "COST", "NKE", "SBUX", "MCD", "CMG",
    "LULU", "TJX",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "NEE",
    # Industrials / Aerospace
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "BA", "UPS",
    # Materials / Real Estate
    "FCX", "NEM", "LIN", "AMT", "PLD", "EQIX",
    # Crypto-adjacent
    "COIN", "MSTR",
    # ETF Benchmarks
    "SPY", "QQQ", "PLTR",
]

# ── EXPANSION (admitted only past the coverage gate) ──────────────────────────
# Additional S&P-500-class large/mid-caps, grouped by sector. High-liquidity names
# with SEC filings (so EDGAR quality factors resolve). Any that fail to resolve are
# flagged by corporate_actions and excluded by coverage measurement — safe.
_EXPANSION: list[str] = [
    # Tech / Semis / Hardware
    "CSCO", "ACN", "ADI", "LRCX", "KLAC", "SNPS", "CDNS", "ANET", "FTNT",
    "ROP", "APH", "MCHP", "MSI", "GLW", "HPQ", "HPE", "DELL", "WDC", "STX",
    "NXPI", "ON", "MPWR", "TER", "SWKS", "KEYS", "GRMN", "TYL", "PTC", "ANSS",
    "CDW", "IT", "FICO", "AKAM", "JNPR", "FSLR", "ENPH", "TDY", "ZBRA",
    # Software / Internet / Media / Comm
    "INTU", "ADSK", "CRM", "DOCU", "OKTA", "TWLO", "HUBS", "ZM", "DDOG",
    "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "WBD", "PARA", "FOXA", "OMC",
    "IPG", "TTD", "PINS", "SNAP", "RBLX", "DASH", "ROKU", "EA", "TTWO",
    # Financials
    "SCHW", "USB", "PNC", "TFC", "COF", "BK", "STT", "AIG", "MET", "PRU",
    "TRV", "ALL", "PGR", "CB", "AFL", "MMC", "AON", "AJG", "ICE", "CME",
    "SPGI", "MCO", "MSCI", "NDAQ", "FIS", "FISV", "GPN", "DFS", "SYF",
    "AMP", "TROW", "NTRS", "RF", "CFG", "KEY", "HBAN", "FITB", "MTB",
    # Healthcare / Pharma / Biotech / Devices
    "ABT", "MDT", "SYK", "BSX", "BDX", "EW", "ZBH", "BAX", "HCA", "CI",
    "CVS", "ELV", "CNC", "HUM", "MCK", "COR", "CAH", "IQV", "A", "IDXX",
    "MTD", "RMD", "WST", "DXCM", "BIIB", "MRNA", "ZTS",
    "HOLX", "ALGN", "STE", "PODD", "GEHC",
    # Consumer Staples
    "PG", "KO", "PEP", "MDLZ", "PM", "MO", "CL", "KMB", "GIS", "KHC",
    "HSY", "STZ", "KDP", "MNST", "KR", "SYY", "ADM", "MKC", "CHD", "CLX",
    "K", "HRL", "TSN", "CAG", "CPB", "DG", "DLTR", "WBA",
    # Consumer Discretionary
    "AMZN", "ROST", "ORLY", "AZO", "YUM", "MAR", "HLT", "DRI", "GM", "F",
    "APTV", "BWA", "LEN", "DHI", "NVR", "PHM", "GRMN", "EXPE", "RCL", "CCL",
    "NCLH", "WYNN", "LVS", "MGM", "POOL", "ULTA", "BBY", "GPC", "TSCO",
    # Industrials
    "UNP", "CSX", "NSC", "FDX", "GD", "NOC", "LHX", "TDG", "EMR", "ETN",
    "PH", "ITW", "MMM", "ROK", "DOV", "IR", "AME", "FTV", "XYL", "CMI",
    "PCAR", "WM", "RSG", "PWR", "URI", "FAST", "PAYX", "ADP", "VRSK",
    "EFX", "CTAS", "ODFL", "JCI", "CARR", "OTIS", "WAB", "SWK",
    # Energy / Utilities
    "PSX", "MPC", "VLO", "PXD", "HES", "DVN", "FANG", "KMI", "WMB", "OKE",
    "HAL", "BKR", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "PEG",
    "ED", "WEC", "ES", "EIX", "PCG", "AEE", "DTE", "PPL", "FE", "CNP",
    # Materials
    "APD", "SHW", "ECL", "DOW", "DD", "PPG", "NUE", "VMC", "MLM", "CTVA",
    "IFF", "ALB", "CE", "CF", "MOS", "STLD", "PKG", "IP", "BALL", "AVY",
    # Real Estate
    "O", "PSA", "CCI", "SPG", "WELL", "DLR", "VICI", "SBAC", "AVB", "EQR",
    "EXR", "MAA", "INVH", "ARE", "VTR", "IRM", "CBRE",
]

# Build the expanded set: core + expansion, de-duplicated and stably ordered.
# (A few intentional dupes above — e.g. AMZN, DDOG, CRM, GRMN — collapse here.)
EXPANDED_UNIVERSE: list[str] = sorted(set(CORE_UNIVERSE) | set(_EXPANSION))

TARGET_UNIVERSE_SIZE = 400


def get_active_universe(coverage_ok: bool, enabled: bool | None = None) -> list[str]:
    """The ACTIVE scoring/trading universe.

    Returns EXPANDED_UNIVERSE only when expansion is ENABLED and fundamental
    coverage has cleared the IPS 80% floor (`coverage_ok`); otherwise CORE_UNIVERSE.
    Both conditions are required — a wider pool on thin coverage adds momentum-only
    names with no quality/value signal, defeating the Phase 2 goal.

    `enabled` defaults to the UNIVERSE_EXPANDED env flag (default OFF), so shipping
    this module is ZERO behavior change until the operator flips it after observing
    coverage ≥ 80% in the data_quality logs.
    """
    if enabled is None:
        enabled = os.getenv("UNIVERSE_EXPANDED", "").lower() in ("1", "true", "yes")
    if enabled and coverage_ok:
        return EXPANDED_UNIVERSE
    return CORE_UNIVERSE


# ── Resumable fetch cursor (fetch_progress.json) ──────────────────────────────

FETCH_PROGRESS = "fetch_progress.json"


def _load_progress(path: str) -> dict:
    if os.path.isfile(path):
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def _universe_fingerprint(tickers: list[str]) -> str:
    """Stable fingerprint of the exact universe CONTENTS (order-sensitive), so a
    same-length swap (remove one name, add another) is detected as a different
    universe and resets the sweep. Keying on size alone would silently resume the
    old cursor and skip the first N names of the new ordering — a silent coverage gap."""
    import hashlib
    h = hashlib.sha1("\n".join(tickers).encode()).hexdigest()
    return h[:16]


def next_batch(tickers: list[str], batch_size: int,
               path: str = FETCH_PROGRESS) -> tuple[list[str], int]:
    """Return the next ``batch_size`` tickers to fetch and the new cursor position.

    Advances a persisted wrap-around cursor keyed by a fingerprint of the universe
    CONTENTS — any change to the universe (size OR membership/order) resets the cursor
    to 0, so a changed universe starts a fresh sweep instead of resuming mid-way and
    skipping names. Persisting is the caller's job via ``save_batch`` after a
    successful fetch — so a crash mid-fetch does NOT advance the cursor and the same
    batch is retried next run. Returns ``([], 0)`` for an empty universe.
    ``batch_size`` ≥ len → the whole universe.
    """
    n = len(tickers)
    if n == 0 or batch_size <= 0:
        return [], 0
    prog = _load_progress(path)
    cursor = prog.get("cursor", 0)
    if (prog.get("fingerprint") != _universe_fingerprint(tickers)
            or not isinstance(cursor, int) or cursor < 0 or cursor >= n):
        cursor = 0
    end = min(cursor + batch_size, n)
    return tickers[cursor:end], cursor


def save_batch(tickers: list[str], batch_size: int, cursor: int,
               path: str = FETCH_PROGRESS) -> int:
    """Persist the advanced cursor after a batch fetch succeeds. Returns the new
    cursor (wrapped to 0 at the end of the universe). Atomic write. Stores the
    universe fingerprint so a later membership change resets the sweep."""
    n = len(tickers)
    if n == 0 or batch_size <= 0:
        return 0
    new_cursor = cursor + batch_size
    if new_cursor >= n:
        new_cursor = 0            # wrap → next run starts a fresh sweep
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"cursor": new_cursor, "fingerprint": _universe_fingerprint(tickers)}, f, indent=2)
    os.replace(tmp, path)
    return new_cursor
