"""
publish.py — Push daily portfolio snapshot to Supabase.

Called as Step 8 in main.py after trades are logged. Reads local JSON files,
computes metrics, and upserts to three Supabase tables:
  - portfolio_snapshots  (one row per day, keyed by date)
  - trades               (append-only via upsert, keyed by transaction_id)
  - positions            (current holdings, replaced wholesale each run)

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in environment.
Silently skips if either is missing (local dev without Supabase configured).
"""

import json
import math
import os
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import market_calendar

load_dotenv()

_ET = ZoneInfo("America/New_York")

STARTING_CAPITAL = 500.0
TRANSACTIONS_FILE = "transactions.json"
PEAK_FILE = "portfolio_peak.json"
AGENT_LOG_FILE = "agent_log.json"
SNAPSHOT_FILE = "portfolio_snapshot.json"


def _sanitize(obj):
    """Recursively replace NaN/Inf floats with None so every payload is valid JSON.

    Strict JSON parsers — including Supabase/PostgREST — reject NaN and Infinity
    ("Out of range float values are not JSON compliant"), and Python's json module
    emits them as bare NaN/Infinity tokens that also break any consumer re-reading
    the committed portfolio_snapshot.json. A NaN can reach here from a degenerate
    quant calc (e.g. a zero/NaN close producing NaN volatility), so scrub at the
    serialization boundary unconditionally — this is the last line of defense even
    after the upstream quant_engine guard."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _now_et() -> datetime:
    """The current time in US/Eastern. One seam for the benchmark-close logic, so a
    test can pin the clock to a specific side of the 4 PM close."""
    return datetime.now(_ET)


def _load(path: str, default):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return default


# ── Benchmark closes are DATED ────────────────────────────────────────────────
# Third staleness recurrence (found 2026-10-07; ~34 trading-day rows from
# 2026-08-13 on held the PRIOR session's close). Every earlier fix validated one
# source and left another unvalidated. The rule now is structural: no source
# hands back a bare price. Each source yields (session date, close) pairs, and a
# value reaches a row only by looking up the session that row is supposed to
# show. A stale source therefore cannot be mistaken for a fresh one — it simply
# has no bar for the date asked.
BENCHMARK_TICKERS = ("SPY", "QQQ")

# Trailing portfolio_snapshots rows re-checked on every publish. One row exists
# per weekday and the newest (today's) is written by the publish itself, so 15
# rows re-checks the 14 weekdays before today — at least 10 trading days even
# when the window spans holidays.
RECONCILE_LOOKBACK_ROWS = 15

# A stored close is "the same" as a dated bar within half a cent (the column is
# rounded to 4 dp; this is the tolerance backfill_qqq.py already uses).
CLOSE_TOLERANCE = 0.005

# A lagged close is wrong by one session's move. If a stored close and the dated
# bar for its session differ by more than this fraction, the cause is not
# staleness — it is a re-based series (a split re-adjusts every historical bar
# while the stored inception close stays put) or bad data. The repair refuses
# those rows and reports them rather than rewriting history onto a new basis.
MAX_RECONCILE_MOVE = 0.10


def _bar_session_date(epoch_ms) -> date:
    """Session date (ET) of a Polygon daily bar's epoch-ms timestamp.

    Polygon stamps a daily bar at midnight ET of its session (04:00 UTC in EDT,
    05:00 UTC in EST); the same call also maps a 16:00 ET stamp to the right day.
    """
    return datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc).astimezone(_ET).date()


def _iso_date(d) -> date:
    return d if isinstance(d, date) and not isinstance(d, datetime) \
        else date.fromisoformat(str(d)[:10])


def _snapshot_complete_through(snap: dict) -> date | None:
    """Newest session whose close was final when `snap` was fetched.

    A bar is only a CLOSE if its session had ended when the file was fetched. A
    fetch made while the market is open can carry a partial bar for that day
    (Polygon's free tier does not serve one today; a delayed-data tier would),
    so bars newer than this are refused. Prefers the precise `fetched_at`; falls
    back to the file's `date` taken as midnight ET (so the fetch day's own bar is
    never trusted). Neither stamp → None: nothing can be proven complete.
    """
    fetched_at = snap.get("fetched_at")
    if fetched_at:
        try:
            ts = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return market_calendar.most_recent_complete_trading_day(ts.astimezone(_ET))
        except ValueError:
            pass
    file_date = snap.get("date")
    if file_date:
        try:
            return market_calendar.previous_trading_day(str(file_date)[:10])
        except ValueError:
            pass
    return None


def _snapshot_benchmark_bars(ticker: str, snap: dict | None = None) -> dict[date, float]:
    """{session date: close} for `ticker` from market_snapshot.json's dated history.

    Reads `history[ticker]` — each bar carries its own epoch-ms session stamp —
    and never `prices[ticker].close`, which has no date of its own: it sits under
    the file's FETCH date while holding the prior session's close. That mismatch
    is the 2026-08-13 → 2026-10-07 bug. Returns {} on any problem.
    """
    try:
        if snap is None:
            snap = _load("market_snapshot.json", {})
        if not isinstance(snap, dict):
            return {}
        through = _snapshot_complete_through(snap)
        if through is None:
            return {}
        out: dict[date, float] = {}
        for bar in (snap.get("history") or {}).get(ticker) or []:
            try:
                stamp = bar.get("date", bar.get("t"))
                close = float(bar.get("close", bar.get("c")))
                if stamp is None or not math.isfinite(close) or close <= 0:
                    continue
                session = _bar_session_date(stamp)
            except (TypeError, ValueError, AttributeError):
                continue
            if session <= through:
                out[session] = close
        return out
    except Exception:
        return {}


def _fetch_benchmark_from_snapshot(ticker: str = "SPY", expected: date | None = None) -> float | None:
    """`ticker`'s close for session `expected`, from market_snapshot.json's dated bars.

    `expected` defaults to market_calendar.most_recent_complete_trading_day().
    Returns None unless the snapshot holds a bar dated exactly that session.

    History: this used to check only that the snapshot FILE was dated today and
    return `prices[ticker].close`. The file is fetched pre-market, so that price
    is always the prior session's — correct for a morning publish, wrong after
    the close. It was tried first until 2026-07-31 (every EOD row from ~07-06 was
    a day stale), then demoted to a fallback — where, from 2026-08-13 on, it
    silently caught every EOD publish whose Polygon `/prev` bar had been
    correctly rejected as stale, and wrote the prior close anyway.
    """
    if expected is None:
        expected = market_calendar.most_recent_complete_trading_day()
    return _snapshot_benchmark_bars(ticker).get(expected)


def _fetch_benchmark_prev_bar(ticker: str, polygon_key: str, expected: date | None = None,
                              max_retries: int = 3,
                              retry_delay: float = 20.0) -> tuple[date, float] | None:
    """Polygon's `/prev` bar for `ticker` as (session date, close), or None.

    Retries (bounded) while the bar is older than `expected` — Polygon can lag a
    session's finalization — and returns the newest bar it saw EITHER WAY, with
    its real date attached. A bar older than `expected` is not an error here: it
    is a correctly-dated bar for an earlier session, and the caller decides
    which row, if any, it belongs to. `expected` defaults to
    market_calendar.most_recent_complete_trading_day().
    """
    if expected is None:
        expected = market_calendar.most_recent_complete_trading_day()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey={polygon_key}"
    newest: tuple[date, float] | None = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-investor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            results = data.get("results", [])
            if results:
                bar = results[0]
                close = float(bar.get("c", 0))
                bar_ms = bar.get("t")
                if math.isfinite(close) and close > 0 and bar_ms is not None:
                    bar_date = _bar_session_date(bar_ms)
                    if newest is None or bar_date >= newest[0]:
                        newest = (bar_date, close)
                    if bar_date >= expected:
                        return newest
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    return newest


def _fetch_benchmark_prev_close(ticker: str, polygon_key: str, max_retries: int = 3,
                                retry_delay: float = 20.0) -> float | None:
    """Fetch a benchmark's most recently completed session close from Polygon.

    Found live: 2026-07-31 through 2026-08-05 published one trading day stale
    despite the 07-31 "call live Polygon first" fix (34775c4). Root cause: that
    fix never validated WHAT the live call returned — Polygon's "prev" endpoint
    can lag its own session's finalization, so a call made right after the close
    can still return YESTERDAY's close. A late, stale-but-200-OK response was
    indistinguishable from a fresh one, so it was accepted and permanently
    written. The returned bar's own date is validated against
    market_calendar.most_recent_complete_trading_day(); a stale bar is retried
    with a bounded backoff rather than accepted, and if it is still stale this
    returns None.

    Kept as the validated single-value form of `_fetch_benchmark_prev_bar`. The
    publish path uses the dated form directly (see `_collect_benchmark_bars`).
    """
    expected = market_calendar.most_recent_complete_trading_day()
    bar = _fetch_benchmark_prev_bar(ticker, polygon_key, expected=expected,
                                    max_retries=max_retries, retry_delay=retry_delay)
    if bar is None or bar[0] < expected:
        return None
    return bar[1]


def _collect_benchmark_bars(polygon_key: str | None) -> tuple[dict, dict]:
    """Every dated benchmark bar this run can see, plus where each source stands.

    Returns (bars, provenance):
      bars[ticker]       = {session date: close}  — snapshot history, overlaid with
                           Polygon's `/prev` bar (the only source that can hold a
                           session newer than the pre-market snapshot)
      provenance[ticker] = {"polygon_prev": date | None, "snapshot_latest": date | None}
                           — the newest session each source offered, for the log
                           line when a row cannot be filled.
    No new Polygon calls versus before: still one `/prev` per ticker.
    """
    bars: dict[str, dict[date, float]] = {}
    provenance: dict[str, dict] = {}
    newest_complete = market_calendar.most_recent_complete_trading_day(_now_et())
    for ticker in BENCHMARK_TICKERS:
        by_date = _snapshot_benchmark_bars(ticker)
        prov = {"polygon_prev": None, "snapshot_latest": max(by_date) if by_date else None}
        if polygon_key:
            prev = _fetch_benchmark_prev_bar(ticker, polygon_key, expected=newest_complete)
            if prev is not None:
                by_date[prev[0]] = prev[1]
                prov["polygon_prev"] = prev[0]
        bars[ticker] = by_date
        provenance[ticker] = prov
    return bars, provenance


def _expected_session(row_date, now_et: datetime | None = None) -> date:
    """The session a row dated `row_date` must show: the most recent COMPLETED
    session on or before that date.

      trading day, before its 4 PM close  → the prior session (provisional as-of)
      trading day, at/after its close     → that day
      weekend / holiday                   → the last session before it
      a past date                         → that day's own session (or the last one
                                            before it), never a later one — a late
                                            re-publish must not put a newer close on
                                            an older row
    """
    d = _iso_date(row_date)
    as_of = d if market_calendar.is_trading_day(d) else market_calendar.previous_trading_day(d)
    complete = market_calendar.most_recent_complete_trading_day(now_et or _now_et())
    return min(as_of, complete)


def _resolve_benchmark_closes(bars: dict, session: date) -> dict[str, float] | None:
    """{ticker: close} for `session`, or None unless EVERY benchmark has a bar dated
    exactly `session`. All-or-nothing keeps SPY and QQQ on one as-of date: the
    dashboard never measures the portfolio against two different sessions."""
    out = {}
    for ticker in BENCHMARK_TICKERS:
        close = (bars.get(ticker) or {}).get(session)
        if close is None:
            return None
        out[ticker] = close
    return out


def _describe_stale_sources(provenance: dict, session: date) -> str:
    parts = []
    for ticker in BENCHMARK_TICKERS:
        prov = provenance.get(ticker) or {}
        parts.append(f"{ticker}: polygon prev={prov.get('polygon_prev') or 'none'}, "
                     f"snapshot latest={prov.get('snapshot_latest') or 'none'}")
    return f"no bar dated {session} for both benchmarks ({'; '.join(parts)})"


# A4 — benchmark total-return gross-up. The portfolio curve is dividend-inclusive
# (dividends land as account cash), so the dashboard must benchmark against
# TOTAL return, not price return, or it flatters the portfolio by ~the dividend
# yield. The snapshot carries only raw closes, so we add the accrued dividend as
# a documented gross-up, pro-rated by days since inception. Written into the
# *_cumulative_return_pct columns.
#
# SPY ~1.25%/yr (S&P 500). QQQ ~0.45%/yr — the Nasdaq 100 is growth-heavy and
# pays materially less, so it gets its own rate rather than borrowing SPY's;
# using 1.25% for QQQ would hand the Nasdaq benchmark ~0.8%/yr of return it
# never paid, which biases the comparison AGAINST the portfolio.
SPY_DIVIDEND_YIELD = 0.0125
QQQ_DIVIDEND_YIELD = 0.0045

BENCHMARK_DIVIDEND_YIELD = {"SPY": SPY_DIVIDEND_YIELD, "QQQ": QQQ_DIVIDEND_YIELD}


def _benchmark_inception(supabase_client, ticker: str) -> tuple[str, float] | None:
    """(row date, close) of `ticker`'s inception row — the first portfolio_snapshots
    row with a non-null <ticker>_close — or None when no row has one yet.

    Resolved per-benchmark. SPY and QQQ share the same inception date today (both
    backfilled from 2026-06-08), but keying off each column's own first non-null
    row means a benchmark added later still baselines correctly instead of
    silently inheriting SPY's start date. Raises on a query error.
    """
    col = f"{ticker.lower()}_close"
    resp = (
        supabase_client.table("portfolio_snapshots")
        .select(f"{col}, date")
        .not_.is_(col, "null")
        .order("date", desc=False)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    return str(rows[0].get("date"))[:10], float(rows[0][col])


def _benchmark_cumulative_pct(ticker: str, close: float | None, inception_close: float,
                              inception_date, as_of) -> float | None:
    """Benchmark cumulative TOTAL return (%) at `as_of`: price return from
    `inception_close` to `close`, plus the dividend gross-up for the calendar
    days elapsed since `inception_date`. THE formula — the live publish, the
    trailing reconcile and the one-time backfill all go through this function,
    so a recomputed row can never disagree with a freshly published one.
    """
    if close is None or inception_close is None or inception_close <= 0:
        return None
    div_yield = BENCHMARK_DIVIDEND_YIELD.get(ticker.upper(), 0.0)
    price_ret = (close - inception_close) / inception_close
    # Dividend gross-up by days since inception (total-return basis).
    try:
        d0 = date.fromisoformat(str(inception_date)[:10])
        d1 = date.fromisoformat(str(as_of)[:10]) if as_of else datetime.now(_ET).date()
        div = div_yield * max(0, (d1 - d0).days) / 365.0
    except (ValueError, TypeError):
        div = 0.0
    return round((price_ret + div) * 100, 4)


def _get_benchmark_cumulative(supabase_client, ticker: str, close: float | None,
                              today: str | None = None) -> float | None:
    """Benchmark cumulative TOTAL return (%) vs inception (first row with a
    non-null <ticker>_close). Price return from closes + a dividend gross-up by
    days elapsed. See `_benchmark_inception` / `_benchmark_cumulative_pct`.
    """
    if close is None:
        return None
    try:
        inception = _benchmark_inception(supabase_client, ticker)
        if inception is None:
            return 0.0  # first snapshot with this benchmark's data — baseline = 0%
        return _benchmark_cumulative_pct(ticker, close, inception[1], inception[0], today)
    except Exception:
        return None


# ── Self-healing: re-check rows already written ───────────────────────────────
# The first two staleness incidents were each repaired by hand and each came
# back, because a row was written once and never looked at again. Now every
# publish re-derives what the trailing rows SHOULD hold from dated bars and
# corrects any that differ, so a wrong or NULL close lasts until the next
# publish that can see that session's bar — normally the next morning's.

BENCHMARK_COLUMNS = tuple(
    c for t in BENCHMARK_TICKERS
    for c in (f"{t.lower()}_close", f"{t.lower()}_cumulative_return_pct")
)


def plan_benchmark_reconcile(rows: list[dict], bars: dict, inceptions: dict,
                             now_et: datetime | None = None,
                             exclude_dates: tuple = ()) -> dict:
    """Pure planner: which rows' benchmark columns are wrong, and what they should be.

    rows        portfolio_snapshots rows (need `date` + the four benchmark columns)
    bars        {ticker: {session date: close}}       (see _collect_benchmark_bars)
    inceptions  {ticker: (inception row date, inception close)}

    Returns {"changes": [...], "unverifiable": [...], "inception_notes": [...],
    "implausible": [...]}.
    A change is {"date", "session", "old": {col: v}, "new": {col: v}} and carries
    ONLY benchmark columns — never close_value or anything else.

    Rules:
      • A row is checked against the bar for `_expected_session(row date)`, so a
        holiday/weekend row keeps the as-of (last completed session) close.
      • SPY and QQQ must BOTH have a bar for that session or the row is left
        alone and listed as unverifiable — never half-corrected.
      • A close is changed when it is NULL or differs from the bar by more than
        CLOSE_TOLERANCE; its cumulative return is recomputed with it. A row whose
        close already matches is not touched, whatever its cumulative says.
      • A non-NULL close that differs from its bar by more than MAX_RECONCILE_MOVE
        is not a one-session lag; it is refused and listed as implausible.
      • The inception row, and anything dated before it, is never modified: every
        cumulative return is measured from that row, so changing it would silently
        rebase the whole curve. A mismatch there is reported for the owner instead.
    """
    changes, unverifiable, inception_notes, implausible = [], [], [], []
    excluded = {str(d)[:10] for d in exclude_dates}
    for row in sorted(rows, key=lambda r: str(r.get("date"))):
        row_date = str(row.get("date"))[:10]
        if row_date in excluded:
            continue
        try:
            session = _expected_session(row_date, now_et)
        except ValueError:
            continue
        closes = _resolve_benchmark_closes(bars, session)
        if closes is None:
            unverifiable.append({"date": row_date, "session": session.isoformat()})
            continue
        old, new = {}, {}
        for ticker in BENCHMARK_TICKERS:
            close_col = f"{ticker.lower()}_close"
            cum_col = f"{ticker.lower()}_cumulative_return_pct"
            stored = row.get(close_col)
            matches = stored is not None and abs(float(stored) - closes[ticker]) <= CLOSE_TOLERANCE
            inception = inceptions.get(ticker)
            if inception is None:
                continue
            if row_date <= inception[0]:
                if row_date == inception[0] and not matches:
                    inception_notes.append({
                        "date": row_date, "ticker": ticker, "session": session.isoformat(),
                        "stored_close": stored, "bar_close": round(closes[ticker], 4),
                        "note": "inception row differs from its dated bar — NOT changed "
                                "(it is the baseline of every cumulative return)",
                    })
                continue
            if matches:
                continue
            if stored is not None and float(stored) > 0 and \
                    abs(closes[ticker] / float(stored) - 1.0) > MAX_RECONCILE_MOVE:
                implausible.append({
                    "date": row_date, "ticker": ticker, "session": session.isoformat(),
                    "stored_close": stored, "bar_close": round(closes[ticker], 4),
                    "note": f"differs by more than {MAX_RECONCILE_MOVE:.0%} — not a "
                            "one-session lag (re-based series or bad data); NOT changed",
                })
                continue
            old[close_col], old[cum_col] = stored, row.get(cum_col)
            new[close_col] = round(closes[ticker], 4)
            new[cum_col] = _benchmark_cumulative_pct(
                ticker, closes[ticker], inception[1], inception[0], row_date)
        if new:
            changes.append({"date": row_date, "session": session.isoformat(),
                            "old": old, "new": new})
    return {"changes": changes, "unverifiable": unverifiable,
            "inception_notes": inception_notes, "implausible": implausible}


def apply_benchmark_changes(supabase_client, changes: list[dict]) -> int:
    """Write planned benchmark corrections. UPDATE by date, not upsert, and only
    the benchmark columns in each change — so close_value and every other column
    on the row are structurally out of reach."""
    written = 0
    for change in changes:
        payload = {k: v for k, v in change["new"].items() if k in BENCHMARK_COLUMNS}
        if not payload:
            continue
        (supabase_client.table("portfolio_snapshots")
         .update(_sanitize(payload)).eq("date", change["date"]).execute())
        written += 1
    return written


def reconcile_benchmark_closes(supabase_client, bars: dict,
                               lookback_rows: int | None = RECONCILE_LOOKBACK_ROWS,
                               now_et: datetime | None = None,
                               exclude_dates: tuple = (), apply: bool = True) -> dict:
    """Re-check the trailing `lookback_rows` snapshot rows against dated bars and
    correct the ones that are wrong (`lookback_rows=None` → every row).

    Returns the plan from `plan_benchmark_reconcile` plus {"written": n}. With
    `apply=False` nothing is written. Raises on a Supabase error — callers on the
    publish path wrap it, because a failed repair must never fail a publish.
    """
    query = (supabase_client.table("portfolio_snapshots")
             .select("date, " + ", ".join(BENCHMARK_COLUMNS))
             .order("date", desc=True))
    if lookback_rows is not None:
        query = query.limit(lookback_rows)
    rows = query.execute().data or []
    inceptions = {}
    for ticker in BENCHMARK_TICKERS:
        inception = _benchmark_inception(supabase_client, ticker)
        if inception is not None:
            inceptions[ticker] = inception
    plan = plan_benchmark_reconcile(rows, bars, inceptions, now_et=now_et,
                                    exclude_dates=exclude_dates)
    plan["rows_checked"] = len(rows)
    plan["written"] = apply_benchmark_changes(supabase_client, plan["changes"]) if apply else 0
    return plan


def _print_reconcile(plan: dict, applied: bool = True) -> None:
    """Log what the repair did. Anything it could NOT settle is also raised as a
    GitHub Actions warning annotation: a repair that silently stops working is
    how the first two fixes for this bug decayed unnoticed."""
    verb = "corrected" if applied else "would correct"
    for change in plan["changes"]:
        bits = []
        for ticker in BENCHMARK_TICKERS:
            col = f"{ticker.lower()}_close"
            if col in change["new"]:
                bits.append(f"{ticker} {change['old'].get(col)} → {change['new'][col]}")
        print(f"   🔧 benchmark {verb} {change['date']} (session {change['session']}): "
              + "; ".join(bits))
    problems = []
    for note in plan["inception_notes"]:
        problems.append(f"inception row {note['date']} {note['ticker']}: stored "
                        f"{note['stored_close']} vs bar {note['bar_close']} — not changed")
    for note in plan.get("implausible", []):
        problems.append(f"{note['date']} {note['ticker']}: stored {note['stored_close']} vs "
                        f"bar {note['bar_close']} — {note['note']}")
    # A row waiting on the newest session's bar is the normal after-close gap, not
    # a problem; a row still unverifiable for an OLDER session is.
    newest = market_calendar.most_recent_complete_trading_day(_now_et()).isoformat()
    stuck = [u["date"] for u in plan["unverifiable"] if u["session"] < newest]
    waiting = [u["date"] for u in plan["unverifiable"] if u["session"] >= newest]
    if stuck:
        problems.append(f"{len(stuck)} row(s) have no dated bar for both benchmarks and were "
                        f"left as is: {', '.join(stuck)}")
    if waiting:
        print(f"   ⏳ benchmark reconcile: {', '.join(waiting)} waiting on the "
              f"{newest} bar.")
    for problem in problems:
        print(f"   ⚠ benchmark reconcile: {problem}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=Benchmark close reconcile::{problem}")


def reconcile_benchmarks_main() -> int:
    """`python publish.py --reconcile-benchmarks` — the repair on its own, with no
    snapshot upsert. A callable entry point so a workflow can run it as one step
    (e.g. an evening run that closes the same-day gap once Polygon has the bar).
    Touches only the four benchmark columns of rows that already exist."""
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print("   DRY_RUN — skipping benchmark reconcile (no Supabase writes).")
        return 0
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        print("   Supabase not configured — skipping benchmark reconcile.")
        return 0
    from supabase import create_client
    client = create_client(supabase_url, supabase_key)
    bars, _ = _collect_benchmark_bars(os.getenv("POLYGON_API_KEY"))
    plan = reconcile_benchmark_closes(client, bars)
    _print_reconcile(plan)
    print(f"   ✅ Benchmark reconcile: {plan['rows_checked']} row(s) checked, "
          f"{plan['written']} corrected.")
    return 0


def _publish_quant_scores(client, quant_scores: dict, today: str) -> None:
    sorted_tickers = sorted(
        [(t, s) for t, s in quant_scores.items() if s.get("data_available", False)],
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )
    rows = [
        {
            "date":       today,
            "ticker":     ticker,
            "rank":       rank + 1,
            "composite":  scores.get("composite_score"),
            "momentum":   scores.get("momentum_score"),
            "quality":    scores.get("quality_score"),
            "valuation":  scores.get("valuation_score"),
            "volatility": scores.get("volatility_score"),
            "return_1m":  scores.get("return_1m"),
            "return_3m":  scores.get("return_3m"),
            "return_6m":  scores.get("return_6m"),
            "ann_vol":    scores.get("volatility"),
            "beta":       scores.get("beta"),
        }
        for rank, (ticker, scores) in enumerate(sorted_tickers)
    ]
    if rows:
        client.table("quant_scores").upsert(_sanitize(rows), on_conflict="date,ticker").execute()
        print(f"   📊 {len(rows)} quant score(s) synced.")


def publish_to_supabase(portfolio: dict | None = None, quant_scores: dict | None = None,
                        is_close: bool = False, regime: str | None = None) -> None:
    # ── Load portfolio_snapshot.json (written by cloud routine, read by GH Actions) ─
    file_snapshot = _load(SNAPSHOT_FILE, {})

    # is_close: explicit arg wins. File fallback only applies in GH Actions, where
    # publish.py is invoked directly (not via main.py) and needs to read the flag
    # from portfolio_snapshot.json committed by the cloud routine. Without the
    # GITHUB_ACTIONS guard the morning daily-cycle run would inherit is_close=True
    # from the previous day's EOD file, writing close_value prematurely.
    if not is_close and os.environ.get("GITHUB_ACTIONS"):
        is_close = bool(file_snapshot.get("is_close", False))

    # ── Portfolio state: arg → mcp_portfolio.json → snapshot ─────────────────
    # mcp_portfolio.json is written fresh by each routine run and always reflects
    # the current broker state. The embedded snapshot portfolio is stale by the
    # time the EOD routine calls publish.py — it was written at pipeline start.
    # GitHub Actions never has mcp_portfolio.json (gitignored), so it falls
    # through to the snapshot, which is correct for that path.
    if portfolio is None:
        mcp = _load("mcp_portfolio.json", None)
        portfolio = mcp or file_snapshot.get("portfolio") or \
            {"cash": STARTING_CAPITAL, "total_value": STARTING_CAPITAL, "positions": []}

    # ── Quant scores: arg → snapshot ──────────────────────────────────────────
    if quant_scores is None:
        quant_scores = file_snapshot.get("quant_scores")

    total_value = float(portfolio.get("total_value", STARTING_CAPITAL))
    cash        = float(portfolio.get("cash", STARTING_CAPITAL))
    positions   = portfolio.get("positions", [])

    # ── Regime: explicit arg → today's agent_log.json → snapshot file ─────────
    # Priority matters. main.py passes the LIVE pipeline regime as `regime=`. The
    # standalone GH Actions / EOD path has no arg, so it reads the latest
    # agent_log entry (committed by the routine THIS run). The snapshot file is the
    # last resort ONLY — it can hold a PRIOR day's regime (e.g. the EOD snapshot's
    # "NEUTRAL"), and because any non-empty string is truthy, trusting it first
    # silently published a stale regime to the live dashboard (RISK_ON run shown as
    # NEUTRAL). Always prefer a fresher source.
    if not regime:
        logs = _load(AGENT_LOG_FILE, [])
        if logs:
            last_log = logs[-1]
            regime_data = last_log.get("regime", {})
            # Only trust agent_log if its run is from today (ET) — otherwise it is
            # just as stale as the snapshot file and we fall through.
            log_date = str(last_log.get("date", ""))[:10]
            today_et = datetime.now(_ET).strftime("%Y-%m-%d")
            if isinstance(regime_data, dict) and log_date == today_et:
                regime = regime_data.get("regime", "")
    if not regime:
        regime = file_snapshot.get("regime", "")

    # ── Write portfolio_snapshot.json for GH Actions trigger ─────────────────
    # Supabase is blocked in Anthropic's cloud, so the cloud routine writes this
    # file and commits it. The push triggers publish.yml in GitHub Actions, which
    # has Supabase access. GITHUB_ACTIONS guard prevents an infinite trigger loop.
    if not os.environ.get("GITHUB_ACTIONS"):
        try:
            with open(SNAPSHOT_FILE, "w") as _sf:
                json.dump(
                    _sanitize({
                        "is_close":     is_close,
                        "portfolio":    portfolio,
                        "quant_scores": quant_scores,
                        "regime":       regime,
                        "written_at":   datetime.now(timezone.utc).isoformat(),
                    }),
                    _sf,
                    indent=2,
                    allow_nan=False,  # fail loudly if a NaN ever slips past _sanitize
                )
        except Exception as _e:
            print(f"   ⚠️  Could not write {SNAPSHOT_FILE}: {_e}")

    # ── DRY_RUN never performs live Supabase writes ──────────────────────────────
    # A local dry run (main.py or risk_watch.py with DRY_RUN=true) must not touch the
    # production website's data. Placed AFTER the portfolio_snapshot.json write above
    # (that file is the trigger for the GitHub Actions publish.yml, which does the
    # REAL write with no DRY_RUN set — .env is gitignored, so GH Actions has no
    # DRY_RUN) and BEFORE any Supabase network call, so the cloud publish flow is
    # unaffected — only the in-process network write is suppressed. In the Anthropic
    # cloud (DRY_RUN=true, Supabase egress-blocked) this now returns cleanly instead
    # of raising a 403 that has to be reclassified downstream.
    # Found 2026-07-05: a local DRY_RUN risk_watch dry run published a synthetic
    # portfolio ($300 / -40%) to the live production Supabase.
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print("   DRY_RUN — skipping live Supabase write "
              "(portfolio_snapshot.json written; GitHub Actions publish.yml does the real write).")
        return

    # ── Supabase connection ────────────────────────────────────────────────────
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        print("   Supabase not configured — skipping publish.")
        return

    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
    except ImportError:
        print("   supabase package not installed — skipping publish. Run: pip install supabase")
        return

    # ── Drawdown ───────────────────────────────────────────────────────────────
    peak_data = _load(PEAK_FILE, {})
    peak      = float(peak_data.get("peak", total_value))
    drawdown  = max(0.0, (peak - total_value) / peak * 100) if peak > 0 else 0.0

    # ── Benchmarks: SPY (S&P 500) + QQQ (Nasdaq 100) ───────────────────────────
    # Gather every DATED bar this run can see: the snapshot's history (through the
    # prior session on a normal pre-market fetch) plus Polygon's live "prev" bar
    # (the only source that can hold today's close after 4 PM). Nothing here is a
    # bare price — the row's close is looked up by session date further down, once
    # the row's date is known, so a stale source cannot pass for a fresh one.
    polygon_key = os.getenv("POLYGON_API_KEY")
    benchmark_bars, benchmark_provenance = _collect_benchmark_bars(polygon_key)

    # ── Upsert portfolio snapshot ──────────────────────────────────────────────
    # When GitHub Actions publishes a snapshot committed after midnight UTC,
    # datetime.now() would return the wrong date. Use written_at from the
    # snapshot file as the authoritative date when available.
    snapshot_written_at = file_snapshot.get("written_at", "")
    if snapshot_written_at and os.environ.get("GITHUB_ACTIONS"):
        today = snapshot_written_at[:10]
    else:
        today = _now_et().strftime("%Y-%m-%d")  # ET matches the rest of the pipeline
    # The session this row must show — the most recent COMPLETED session on or
    # before the row's date — and both benchmarks' closes for exactly that session.
    # All-or-nothing: SPY and QQQ always share one as-of date, or neither is set.
    try:
        benchmark_session = _expected_session(today)
    except ValueError:   # an unparseable row date must not break the publish
        benchmark_session = market_calendar.most_recent_complete_trading_day(_now_et())
    benchmark_closes = _resolve_benchmark_closes(benchmark_bars, benchmark_session)
    spy_close = benchmark_closes["SPY"] if benchmark_closes else None
    qqq_close = benchmark_closes["QQQ"] if benchmark_closes else None
    # Benchmark cumulatives on a TOTAL-return basis (A4) — need `today` for the
    # dividend gross-up, so they are computed after the date is resolved.
    spy_cumulative = _get_benchmark_cumulative(client, "SPY", spy_close, today=today)
    qqq_cumulative = _get_benchmark_cumulative(client, "QQQ", qqq_close, today=today)
    cumulative_return = round((total_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 4)

    snapshot_row: dict = {
        "date":                      today,
        "total_value":               round(total_value, 2),
        "cash":                      round(cash, 2),
        "num_positions":             len(positions),
        "cumulative_return_pct":     cumulative_return,
        "drawdown_pct":              round(drawdown, 4),
        "regime":                    regime or None,
        "updated_at":                datetime.now(timezone.utc).isoformat(),
    }
    if is_close:
        # close_value is the authoritative 4 PM close and must be immutable. A
        # second is_close publish (EOD retry, DST double-fire, manual dispatch)
        # must NOT overwrite it. Only write when today's row has no close_value yet.
        already_closed = False
        try:
            existing = (
                client.table("portfolio_snapshots")
                .select("close_value")
                .eq("date", today)
                .execute()
            )
            rows = existing.data or []
            already_closed = bool(rows) and rows[0].get("close_value") is not None
        except Exception as e:
            print(f"   ⚠️  Could not check existing close_value — {e}. Proceeding to write.")
        if already_closed:
            print(f"   🔒 close_value already set for {today} — preserving immutable close.")
        else:
            snapshot_row["close_value"] = round(total_value, 2)
            snapshot_row["close_at"]    = datetime.now(timezone.utc).isoformat()
    if is_close:
        # The close publish is the last word on today's row until the next repair,
        # so it writes all four benchmark columns EXPLICITLY — a value or NULL. With
        # no bar dated for this session, NULL replaces whatever the morning publish
        # left there (the prior session's close, correct as-of 9:45 AM and wrong as
        # a close). NULL is honest; a prior-session close presented as today's is
        # not. The reconcile below fills it on the next publish that has the bar.
        snapshot_row["spy_close"] = round(spy_close, 4) if spy_close is not None else None
        snapshot_row["spy_cumulative_return_pct"] = spy_cumulative
        snapshot_row["qqq_close"] = round(qqq_close, 4) if qqq_close is not None else None
        snapshot_row["qqq_cumulative_return_pct"] = qqq_cumulative
        if benchmark_closes is None:
            print(f"   ⚠ benchmark close for {today}: "
                  f"{_describe_stale_sources(benchmark_provenance, benchmark_session)} — "
                  "writing NULL for SPY and QQQ; the next publish reconciles it.")
    else:
        # Non-close (morning) publish: unchanged — set what is known, and leave the
        # columns out of the upsert otherwise so an existing value is not blanked.
        if spy_close is not None:
            snapshot_row["spy_close"] = round(spy_close, 4)
        if spy_cumulative is not None:
            snapshot_row["spy_cumulative_return_pct"] = spy_cumulative
        if qqq_close is not None:
            snapshot_row["qqq_close"] = round(qqq_close, 4)
        if qqq_cumulative is not None:
            snapshot_row["qqq_cumulative_return_pct"] = qqq_cumulative
        if benchmark_closes is None:
            print(f"   ⚠ benchmark close for {today}: "
                  f"{_describe_stale_sources(benchmark_provenance, benchmark_session)} — "
                  "benchmark columns left as they are.")

    # A4: net exposure (point-in-time) + realized beta (trailing, best-effort).
    # net_exposure is exact from this snapshot; realized_beta needs a return
    # history, reused from performance.build_report (reads committed agent_log +
    # market_snapshot). Both are optional columns — skip silently if unavailable
    # so a missing column or a too-short history never breaks the publish.
    if total_value:
        snapshot_row["net_exposure"] = round(1.0 - cash / total_value, 4)
    try:
        from performance import build_report
        _rep = build_report()
        beta = _rep.get("realized_beta")
        # Only a beta measured on DAILY 4 PM closes is publishable. Without git history
        # (a shallow checkout) the report falls back to weekly run marks, whose beta is
        # not comparable — skip it rather than publish a different measurement.
        if not str(_rep.get("curve_source", "")).startswith("eod_close"):
            print(f"   ⚠ realized_beta skipped: curve is {_rep.get('curve_source')!r}, "
                  "not daily closes (shallow git history?)")
        elif beta is not None and math.isfinite(beta):
            snapshot_row["realized_beta"] = beta
    except Exception as e:
        print(f"   ⚠ realized_beta skipped: {str(e)[:120]}")

    # Defensive against deploy ordering: a migration-gated column that has not
    # been created yet doesn't exist, and the upsert errors on the whole row.
    # Retry without whichever optional group the error names, so a migration the
    # owner hasn't run degrades the dashboard rather than breaking the publish.
    _OPTIONAL_GROUPS = (
        (("net_exposure", "realized_beta"),
         "net_exposure/realized_beta", "migrations/2026-06-14_add_exposure_beta.sql"),
        (("qqq_close", "qqq_cumulative_return_pct"),
         "qqq_close/qqq_cumulative_return_pct", "migrations/2026-08-22_add_qqq_benchmark.sql"),
    )
    snapshot_row = _sanitize(snapshot_row)
    for attempt in range(len(_OPTIONAL_GROUPS) + 1):
        try:
            client.table("portfolio_snapshots").upsert(snapshot_row).execute()
            break
        except Exception as e:
            msg = str(e)
            hit = next(
                (g for g in _OPTIONAL_GROUPS
                 if any(k in msg for k in g[0]) and any(k in snapshot_row for k in g[0])),
                None,
            )
            if hit is None:
                raise
            keys, label, migration = hit
            for k in keys:
                snapshot_row.pop(k, None)
            print(f"   ⚠ {label} columns missing — run {migration}. "
                  "Publishing without them.")
    print(f"   📊 Snapshot published: value=${total_value:,.2f} return={cumulative_return:+.2f}%"
          + (f" spy={spy_cumulative:+.2f}%" if spy_cumulative is not None else "")
          + (f" qqq={qqq_cumulative:+.2f}%" if qqq_cumulative is not None else ""))

    # ── Self-heal earlier rows' benchmark closes ──────────────────────────────
    # Today's row was just written above from the same dated bars, so it is
    # excluded here. A failed repair is logged and never fails the publish.
    try:
        _print_reconcile(reconcile_benchmark_closes(client, benchmark_bars,
                                                    exclude_dates=(today,)))
    except Exception as e:
        print(f"   ⚠ benchmark reconcile skipped: {str(e)[:160]}")

    # ── Upsert positions (atomic: upsert current, then delete stale) ─────────
    # Avoids the delete-all + insert pattern which leaves the table empty if
    # the insert fails after the delete has already committed.
    pos_rows = []
    for p in positions:
        ticker        = p.get("symbol", "")
        qty           = float(p.get("qty", 0))
        avg_cost      = float(p.get("avg_price", 0))
        current_price = float(p.get("current_price", 0))
        market_value  = float(p.get("market_value", 0))

        unrealized_pct = 0.0
        if avg_cost > 0:
            unrealized_pct = round((current_price - avg_cost) / avg_cost * 100, 4)

        weight_pct = round(market_value / total_value * 100, 4) if total_value > 0 else 0.0

        pos_rows.append({
            "ticker":         ticker,
            "weight_pct":     weight_pct,
            "quantity":       qty,
            "avg_cost":       avg_cost,
            "current_price":  current_price,
            "unrealized_pct": unrealized_pct,
            "updated_at":     datetime.now().isoformat(),
        })

    if pos_rows:
        client.table("positions").upsert(_sanitize(pos_rows), on_conflict="ticker").execute()
        current_tickers = [r["ticker"] for r in pos_rows]
        try:
            client.table("positions").delete().not_.in_("ticker", current_tickers).execute()
        except Exception as e:
            print(f"   ⚠️  Warning: could not delete stale positions — {e}. Stale rows may persist.")
    else:
        # Portfolio is all-cash — clear any stale position rows
        client.table("positions").delete().neq("ticker", "___never___").execute()

    # ── Upsert trades from transactions.json ──────────────────────────────────
    # Exclude dry-run records — they were never actually executed.
    transactions = [tx for tx in _load(TRANSACTIONS_FILE, []) if not tx.get("dry_run")]
    if transactions:
        trade_rows = [
            {
                "id":                  tx.get("transaction_id"),
                "date":                tx.get("date"),
                "ticker":              tx.get("ticker"),
                "action":              tx.get("action"),
                "qty":                 tx.get("qty"),
                "price":               tx.get("price"),
                "total_value":         tx.get("total_value"),
                "target_weight":       tx.get("target_weight"),
                "regime":              tx.get("regime"),
                "rationale":           tx.get("rationale"),
                # Supabase `trades.research_confidence` is an integer column; the agent's
                # 0-10 confidence score is occasionally emitted as a float (e.g. 6.5) —
                # round at the publish boundary rather than trust upstream LLM output to
                # match the exact column type. int(x + 0.5), not round(), since the score
                # is always non-negative and round() half-to-evens 6.5 down to 6.
                "research_confidence": (
                    int(tx["research_confidence"] + 0.5)
                    if isinstance(tx.get("research_confidence"), (int, float))
                    else None
                ),
                **({"broker_order_id": tx["broker_order_id"]} if tx.get("broker_order_id") else {}),
            }
            for tx in transactions
            if tx.get("transaction_id")
        ]
        if trade_rows:
            try:
                client.table("trades").upsert(_sanitize(trade_rows)).execute()
            except Exception as e:
                if "PGRST204" in str(e) and "broker_order_id" in str(e):
                    # PostgREST schema cache hasn't refreshed yet after the column was added;
                    # retry without broker_order_id — column exists in DB and will populate
                    # automatically once the cache catches up on a future run.
                    stripped = [{k: v for k, v in row.items() if k != "broker_order_id"} for row in trade_rows]
                    client.table("trades").upsert(_sanitize(stripped)).execute()
                    print(f"   ⚠️  broker_order_id skipped (schema cache lag) — will populate on next run.")
                else:
                    raise
            print(f"   📋 {len(trade_rows)} trade(s) synced.")

    # ── Quant scores ───────────────────────────────────────────────────────────
    if quant_scores:
        try:
            _publish_quant_scores(client, quant_scores, today)
        except Exception as e:
            print(f"   ⚠️  Quant scores publish failed — {e}")

    print("   ✅ Supabase publish complete.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--close", action="store_true", help="Write close_value (4 PM EOD snapshot)")
    parser.add_argument("--reconcile-benchmarks", action="store_true",
                        help="Only re-check the trailing rows' SPY/QQQ closes against dated "
                             "bars and correct them. No snapshot upsert.")
    args = parser.parse_args()
    if args.reconcile_benchmarks:
        raise SystemExit(reconcile_benchmarks_main())
    publish_to_supabase(is_close=args.close)
