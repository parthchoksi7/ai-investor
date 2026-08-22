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


def _load(path: str, default):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return default


def _fetch_benchmark_from_snapshot(ticker: str = "SPY") -> float | None:
    """Read a benchmark's latest price from market_snapshot.json (committed daily by market_data.yml).

    Fallback only, used when a live Polygon call is unavailable. The snapshot is
    fetched pre-market (7-8:30 AM ET) and never refreshed later in the day, so by
    itself it can never reflect an actual close — a live Polygon "prev" call made
    AT publish time (morning or 4 PM) is always at least as fresh and, after the
    close, strictly fresher. Found live: every EOD publish from ~2026-07-06 to
    2026-07-30 stamped the PRIOR day's close as "today's" spy_close because this
    was tried first and always "succeeded" (fresh file, stale price inside it).

    Returns None if the file is missing, stale (not dated today), or the ticker
    is absent. `fetch_snapshot.py` requests benchmarks=("SPY", "QQQ"), so both
    the S&P 500 and Nasdaq 100 proxies are present in a healthy snapshot.
    """
    try:
        snap = _load("market_snapshot.json", {})
        snap_date = snap.get("date", "")
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        if snap_date != today:
            return None
        bar = snap.get("prices", {}).get(ticker, {})
        close = float(bar.get("close", 0))
        return close if close > 0 else None
    except Exception:
        return None


def _fetch_benchmark_prev_close(ticker: str, polygon_key: str, max_retries: int = 3,
                                retry_delay: float = 20.0) -> float | None:
    """Fetch a benchmark's most recently completed session close from Polygon.

    Found live: 2026-07-31 through 2026-08-05 published one trading day stale
    despite the 07-31 "call live Polygon first" fix (34775c4). Root cause: that
    fix never validated WHAT the live call returned — Polygon's "prev" endpoint
    can lag its own session's finalization by several minutes right at the
    close, so a call made at the EOD routine's exact 4:00:00 PM ET firing time
    can still return YESTERDAY's close even though "prev" is supposed to mean
    TODAY's close from that instant on. A late, stale-but-200-OK response was
    indistinguishable from a fresh one, so it was accepted and permanently
    written — nothing ever re-checked or corrected that date's row afterward.
    Now the returned bar's own date is validated against
    market_calendar.most_recent_complete_trading_day(); a stale bar is retried
    with a bounded backoff (Polygon typically finalizes within ~1 minute of
    close) rather than accepted. If it's still stale after all retries, this
    returns None so the caller falls through to the snapshot fallback (or, if
    that's stale too, leaves the row's <ticker>_close untouched this run) instead
    of writing a value already known to be wrong.
    """
    from market_calendar import most_recent_complete_trading_day
    expected = most_recent_complete_trading_day()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey={polygon_key}"
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
                if close > 0 and bar_ms is not None:
                    bar_date = datetime.fromtimestamp(bar_ms / 1000, tz=timezone.utc).astimezone(_ET).date()
                    if bar_date >= expected:
                        return close
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    return None


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


def _get_benchmark_cumulative(supabase_client, ticker: str, close: float | None,
                              today: str | None = None) -> float | None:
    """Benchmark cumulative TOTAL return (%) vs inception (first row with a
    non-null <ticker>_close). Price return from closes + a dividend gross-up by
    days elapsed.

    NOTE the inception row is resolved per-benchmark. SPY and QQQ share the same
    inception date today (both backfilled from 2026-06-08), but keying off each
    column's own first non-null row means a benchmark added later still baselines
    correctly instead of silently inheriting SPY's start date.
    """
    if close is None:
        return None
    col = f"{ticker.lower()}_close"
    div_yield = BENCHMARK_DIVIDEND_YIELD.get(ticker.upper(), 0.0)
    try:
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
            return 0.0  # first snapshot with this benchmark's data — baseline = 0%
        inception = float(rows[0][col])
        if inception <= 0:
            return None
        price_ret = (close - inception) / inception
        # Dividend gross-up by days since inception (total-return basis).
        div = 0.0
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(str(rows[0].get("date"))[:10])
            d1 = _date.fromisoformat(str(today)[:10]) if today else datetime.now(_ET).date()
            div = div_yield * max(0, (d1 - d0).days) / 365.0
        except (ValueError, TypeError):
            div = 0.0
        return round((price_ret + div) * 100, 4)
    except Exception:
        return None


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
    # Prefer a LIVE Polygon "prev" call over market_snapshot.json. "prev" means
    # "the most recently completed session relative to right now" — called
    # pre-market that's correctly yesterday's close (matches what the snapshot
    # would say anyway), but called after the 4 PM close it correctly flips to
    # TODAY's close, which the pre-market-only snapshot structurally can never
    # have (found live: the old snapshot-first order stamped every EOD publish
    # from ~7/6-7/30 with the PRIOR day's close). Snapshot is the fallback only,
    # for when Polygon itself is unreachable (e.g. no key, or a transient error).
    #
    # QQQ is fetched the same way and on the same schedule as SPY so the two
    # benchmark curves stay aligned to each other: if one is a session stale the
    # other is too, and the dashboard never shows the portfolio measured against
    # two different as-of dates.
    polygon_key = os.getenv("POLYGON_API_KEY")

    def _benchmark_close(ticker: str) -> float | None:
        close = _fetch_benchmark_prev_close(ticker, polygon_key) if polygon_key else None
        return close if close is not None else _fetch_benchmark_from_snapshot(ticker)

    spy_close = _benchmark_close("SPY")
    qqq_close = _benchmark_close("QQQ")

    # ── Upsert portfolio snapshot ──────────────────────────────────────────────
    # When GitHub Actions publishes a snapshot committed after midnight UTC,
    # datetime.now() would return the wrong date. Use written_at from the
    # snapshot file as the authoritative date when available.
    snapshot_written_at = file_snapshot.get("written_at", "")
    if snapshot_written_at and os.environ.get("GITHUB_ACTIONS"):
        today = snapshot_written_at[:10]
    else:
        today = datetime.now(_ET).strftime("%Y-%m-%d")  # ET matches the rest of the pipeline
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
    if spy_close is not None:
        snapshot_row["spy_close"] = round(spy_close, 4)
    if spy_cumulative is not None:
        snapshot_row["spy_cumulative_return_pct"] = spy_cumulative
    if qqq_close is not None:
        snapshot_row["qqq_close"] = round(qqq_close, 4)
    if qqq_cumulative is not None:
        snapshot_row["qqq_cumulative_return_pct"] = qqq_cumulative

    # A4: net exposure (point-in-time) + realized beta (trailing, best-effort).
    # net_exposure is exact from this snapshot; realized_beta needs a return
    # history, reused from performance.build_report (reads committed agent_log +
    # market_snapshot). Both are optional columns — skip silently if unavailable
    # so a missing column or a too-short history never breaks the publish.
    if total_value:
        snapshot_row["net_exposure"] = round(1.0 - cash / total_value, 4)
    try:
        from performance import build_report
        beta = build_report().get("realized_beta")
        if beta is not None and math.isfinite(beta):
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
    args = parser.parse_args()
    publish_to_supabase(is_close=args.close)
