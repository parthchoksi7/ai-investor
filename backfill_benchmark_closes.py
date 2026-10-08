#!/usr/bin/env python3
"""
backfill_benchmark_closes.py — one-time repair of stale SPY/QQQ closes in Supabase.

Found 2026-10-07 (third recurrence): `portfolio_snapshots.spy_close` / `qqq_close`
for row date D held session D-1's close on about 34 trading-day rows (2026-08-07,
2026-08-10, and every row from 2026-08-13), and 2026-08-11 was NULL. The
`*_cumulative_return_pct` columns are derived from them, so the dashboard's
benchmark curves were shifted one session against the portfolio curve.

publish.py now heals the trailing 15 rows on every publish. This script does the
same check over EVERY row, once, for the history that window will never reach.
It uses the same planner (`publish.plan_benchmark_reconcile`) and the same
cumulative-return function as the live path, so a backfilled row is exactly what
a fresh publish would have written.

What it changes: only `spy_close`, `spy_cumulative_return_pct`, `qqq_close`,
`qqq_cumulative_return_pct`, and only on rows whose stored close is NULL or
differs from the dated bar for the session that row should show (a holiday row
keeps the last completed session's close). It never touches `close_value` or any
other column, never inserts a row, and never modifies the inception row (the
baseline of every cumulative return) — a mismatch there is reported instead.

Dated bars come from `market_snapshot.json`'s `history` (git pull first) and,
when POLYGON_API_KEY is set, Polygon's daily aggregates. Where both sources hold
a session they must agree to half a cent; a session they disagree on is treated
as unverifiable and no row is changed from it.

DRY RUN BY DEFAULT. Nothing is written to Supabase without --apply.

Usage:
    python backfill_benchmark_closes.py              # dry run: list + evidence file
    python backfill_benchmark_closes.py --apply      # write exactly the listed rows

Both modes write an evidence file of the rows concerned
(evidence/benchmark_close_backfill_YYYY-MM-DD.json) unless there is nothing to
change. Idempotent: a second run finds nothing and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

import market_calendar
import publish

load_dotenv()

SNAPSHOT_FILE = "market_snapshot.json"
EVIDENCE_DIR = "evidence"


def _polygon_bars(ticker: str, api_key: str, start: date, end: date) -> dict[date, float]:
    """{session date: close} of adjusted daily bars for `ticker` from Polygon."""
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")
    req = urllib.request.Request(url, headers={"User-Agent": "ai-investor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out: dict[date, float] = {}
    for bar in data.get("results") or []:
        close = float(bar.get("c", 0))
        if bar.get("t") is not None and close > 0:
            out[publish._bar_session_date(bar["t"])] = close
    return out


def collect_bars(snapshot_path: str, polygon_key: str | None, first_row_date: date,
                 now_et: datetime | None = None) -> tuple[dict, list[dict], dict]:
    """Dated bars from the snapshot and (optionally) Polygon, cross-checked.

    Returns (bars, mismatches, coverage). A session the two sources disagree on
    is dropped from `bars` — the planner then lists its rows as unverifiable
    rather than picking a side. Only COMPLETED sessions are kept from Polygon,
    so a run during market hours can never backfill a partial bar.
    """
    now_et = now_et or publish._now_et()
    complete = market_calendar.most_recent_complete_trading_day(now_et)
    snap = publish._load(snapshot_path, {})
    bars, mismatches, coverage = {}, [], {}
    for ticker in publish.BENCHMARK_TICKERS:
        from_snapshot = publish._snapshot_benchmark_bars(ticker, snap)
        from_polygon: dict[date, float] = {}
        if polygon_key:
            fetched = _polygon_bars(ticker, polygon_key,
                                    first_row_date - timedelta(days=14), complete)
            from_polygon = {d: c for d, c in fetched.items() if d <= complete}
        merged = dict(from_snapshot)
        for session, close in from_polygon.items():
            other = from_snapshot.get(session)
            if other is not None and abs(other - close) > publish.CLOSE_TOLERANCE:
                mismatches.append({"ticker": ticker, "session": session.isoformat(),
                                   "snapshot_close": other, "polygon_close": close})
                merged.pop(session, None)
            else:
                merged[session] = close
        bars[ticker] = merged
        coverage[ticker] = {
            "snapshot_bars": len(from_snapshot), "polygon_bars": len(from_polygon),
            "first": min(merged).isoformat() if merged else None,
            "last": max(merged).isoformat() if merged else None,
        }
    return bars, mismatches, coverage


def _fmt(v) -> str:
    return "    NULL" if v is None else f"{float(v):8.2f}"


def _fmt_pct(v) -> str:
    return "    NULL" if v is None else f"{float(v):+7.2f}%"


def _print_plan(plan: dict, mismatches: list[dict]) -> None:
    for m in mismatches:
        print(f"  ⚠ sources disagree on {m['ticker']} {m['session']}: snapshot "
              f"{m['snapshot_close']} vs Polygon {m['polygon_close']} — session not used")
    for note in plan["inception_notes"]:
        print(f"  ⚠ inception row {note['date']} {note['ticker']}: stored "
              f"{note['stored_close']} vs bar {note['bar_close']} — NOT changed (owner decision)")
    for note in plan.get("implausible", []):
        print(f"  ⚠ {note['date']} {note['ticker']}: stored {note['stored_close']} vs bar "
              f"{note['bar_close']} — {note['note']}")
    for u in plan["unverifiable"]:
        print(f"  ·  {u['date']}  no agreed bar for both benchmarks (session {u['session']}) "
              "— left as is")
    for change in plan["changes"]:
        print(f"  {change['date']}  session {change['session']}")
        for ticker in publish.BENCHMARK_TICKERS:
            close_col = f"{ticker.lower()}_close"
            cum_col = f"{ticker.lower()}_cumulative_return_pct"
            if close_col in change["new"]:
                print(f"      {ticker}  close {_fmt(change['old'][close_col])} → "
                      f"{_fmt(change['new'][close_col])}   cumulative "
                      f"{_fmt_pct(change['old'][cum_col])} → {_fmt_pct(change['new'][cum_col])}")


def _write_evidence(path: str, plan: dict, mismatches: list[dict], coverage: dict,
                    applied: bool, now_et: datetime) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    doc = {
        "source": "backfill_benchmark_closes.py — portfolio_snapshots benchmark columns "
                  "checked against dated SPY/QQQ daily bars "
                  "(market_snapshot.json history, cross-checked with Polygon when a key is set)",
        "note": "Each change lists the row date, the session that row must show, and the old "
                "and new close and cumulative total return per benchmark. Only these four "
                "columns are written; close_value is never touched.",
        "generated_at": now_et.astimezone(timezone.utc).isoformat(),
        "applied": applied,
        "rows_checked": plan.get("rows_checked"),
        "bar_coverage": coverage,
        "source_mismatches": mismatches,
        "inception_notes": plan["inception_notes"],
        "implausible": plan.get("implausible", []),
        "unverifiable": plan["unverifiable"],
        "changes": plan["changes"],
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(publish._sanitize(doc), f, indent=2)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the listed corrections to Supabase (default: dry run)")
    ap.add_argument("--snapshot", default=SNAPSHOT_FILE,
                    help=f"market snapshot holding dated history (default {SNAPSHOT_FILE})")
    ap.add_argument("--no-polygon", action="store_true",
                    help="use only the snapshot's dated bars, even if POLYGON_API_KEY is set")
    ap.add_argument("--evidence", default=None,
                    help="evidence file path (default evidence/benchmark_close_backfill_<date>.json)")
    args = ap.parse_args(argv)

    if args.apply and os.getenv("DRY_RUN", "false").lower() == "true":
        print("DRY_RUN=true is set — refusing --apply. Unset it to write.")
        return 2
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not (supabase_url and supabase_key):
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set.")
        return 2

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)
    now_et = publish._now_et()

    first = (client.table("portfolio_snapshots").select("date")
             .order("date", desc=False).limit(1).execute().data or [])
    if not first:
        print("No snapshot rows — nothing to backfill.")
        return 0
    polygon_key = None if args.no_polygon else os.getenv("POLYGON_API_KEY")
    try:
        bars, mismatches, coverage = collect_bars(
            args.snapshot, polygon_key, date.fromisoformat(str(first[0]["date"])[:10]), now_et)
    except Exception as e:
        # Type only — a urllib error can echo the request URL, which carries the API key.
        print(f"Could not fetch Polygon bars ({type(e).__name__}). Nothing written. "
              "Retry, or pass --no-polygon to check against the snapshot alone.")
        return 2
    for ticker, cov in coverage.items():
        print(f"{ticker}: {cov['snapshot_bars']} snapshot bar(s), {cov['polygon_bars']} Polygon "
              f"bar(s), sessions {cov['first']} → {cov['last']}")
    if not polygon_key:
        print("(Polygon not used — single-source check against the snapshot's history.)")
    print()

    plan = publish.reconcile_benchmark_closes(client, bars, lookback_rows=None,
                                              now_et=now_et, apply=False)
    _print_plan(plan, mismatches)
    print(f"\n{plan['rows_checked']} row(s) checked · {len(plan['changes'])} to correct · "
          f"{len(plan['unverifiable'])} unverifiable · "
          f"{len(plan.get('implausible', []))} refused as implausible")

    if not plan["changes"]:
        print("Nothing to change — no evidence file written.")
        return 0

    evidence = args.evidence or os.path.join(
        EVIDENCE_DIR, f"benchmark_close_backfill_{now_et.strftime('%Y-%m-%d')}.json")
    if not args.apply:
        _write_evidence(evidence, plan, mismatches, coverage, applied=False, now_et=now_et)
        print(f"DRY RUN — nothing written to Supabase. Evidence: {evidence}")
        print("Re-run with --apply to write exactly the rows listed above.")
        return 0

    written = publish.apply_benchmark_changes(client, plan["changes"])
    _write_evidence(evidence, plan, mismatches, coverage, applied=True, now_et=now_et)
    print(f"✅ Corrected {written} row(s). Evidence: {evidence}")

    # Read back: the same check must now find nothing left to change.
    remaining = publish.reconcile_benchmark_closes(client, bars, lookback_rows=None,
                                                   now_et=now_et, apply=False)["changes"]
    if remaining:
        print(f"❌ {len(remaining)} row(s) still differ after the write: "
              + ", ".join(c["date"] for c in remaining))
        return 1
    print("Verified: a second pass finds nothing to change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
