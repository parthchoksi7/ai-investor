"""Compare one week's Cursor shadow with the live Claude rebalance.

Reads ``shadow/ledger.jsonl`` and the per-day files under ``shadow/``. The
live side is the ``agent_log`` entry and the ``pending_decisions`` envelope
at the ledger's rebalance commit. Nothing here places an order or writes
live state. ``--write`` adds ``shadow/<date>/compare.json``.

Paper prices use the pre-trade portfolio and the snapshot closes the live
run sized on. A later mark, when a newer ``market_snapshot.json`` exists on
the history ref, uses that file's ``prices.close``. Those closes are the
prior completed session (the morning snapshot's ``prices`` block is not the
same-day official close). The figure is a shared mark for both books, not a
fill-level P&L.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def git_text(rev: str, path: str, cwd: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode()


def _orders(decisions) -> list[tuple]:
    out = []
    for d in decisions or []:
        action = str(d.get("action", "")).upper()
        if action in ("BUY", "SELL"):
            out.append((d.get("ticker"), action, d.get("target_weight")))
    return out


def seat_verdicts(state: dict) -> dict:
    """One small verdict per ticker per seat. Theses stay in the seat files."""
    out: dict = {}
    for ticker, row in (state.get("research") or {}).items():
        if isinstance(row, dict):
            out.setdefault(ticker, {})["research_confidence"] = row.get("confidence")
    for ticker, row in (state.get("earnings") or {}).items():
        if isinstance(row, dict):
            out.setdefault(ticker, {})["earnings_alpha"] = row.get("earnings_alpha_score")
    for ticker, row in (state.get("devils_advocate") or {}).items():
        if isinstance(row, dict):
            cell = out.setdefault(ticker, {})
            cell["da_reject"] = row.get("recommend_reject")
            cell["da_risk"] = row.get("overall_risk_score")
    for ticker, row in (state.get("position_reviews") or {}).items():
        if isinstance(row, dict):
            out.setdefault(ticker, {})["position_action"] = row.get("recommended_action")
    return out


def verdict_diffs(cursor: dict, live: dict) -> list[dict]:
    diffs = []
    for ticker in sorted(set(cursor) | set(live)):
        c = cursor.get(ticker) or {}
        v = live.get(ticker) or {}
        changed = {k: {"cursor": c.get(k), "live": v.get(k)}
                   for k in sorted(set(c) | set(v)) if c.get(k) != v.get(k)}
        if changed:
            diffs.append({"ticker": ticker, "fields": changed})
    return diffs


def apply_orders(portfolio: dict, decisions: list, prices: dict) -> tuple[float, dict]:
    """Paper-apply BUY/SELL qty at ``prices[ticker].close``. Returns cash, positions."""
    cash = float(portfolio.get("cash") or 0)
    pos = {}
    for p in portfolio.get("positions") or []:
        sym = p.get("symbol")
        if sym:
            pos[sym] = float(p.get("qty") or 0)
    for d in decisions or []:
        action = str(d.get("action", "")).upper()
        ticker = d.get("ticker")
        if action not in ("BUY", "SELL") or not ticker:
            continue
        try:
            qty = float(d.get("qty") or 0)
            px = float((prices.get(ticker) or {}).get("close") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or px <= 0:
            continue
        held = pos.get(ticker, 0.0)
        if action == "BUY":
            cash -= qty * px
            pos[ticker] = held + qty
        else:
            sell = min(qty, held)
            cash += sell * px
            pos[ticker] = held - sell
    return cash, pos


def mark_to_market(cash: float, positions: dict, prices: dict) -> tuple[float | None, list]:
    total = cash
    missing = []
    for ticker, qty in positions.items():
        if qty <= 1e-9:
            continue
        px = (prices.get(ticker) or {}).get("close")
        if not px:
            missing.append(ticker)
            continue
        total += qty * float(px)
    if missing:
        return None, missing
    return total, []


def _prices_of(rev: str, cwd: str) -> dict | None:
    raw = git_text(rev, "market_snapshot.json", cwd)
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return snap.get("prices") or {}


def _later_snapshot(rebalance_sha: str, cwd: str) -> str | None:
    """First later commit on origin/main (or HEAD) that touches the snapshot."""
    ref = "origin/main"
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=cwd, capture_output=True, check=False,
    )
    if probe.returncode != 0:
        ref = "HEAD"
    proc = subprocess.run(
        ["git", "log", ref, f"--not", rebalance_sha, "--pretty=%H", "--", "market_snapshot.json"],
        cwd=cwd, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    shas = [s for s in proc.stdout.decode().split() if s]
    # git log is newest first; the oldest descendant is the next snapshot.
    return shas[-1] if shas else None


def _live_state(commit: str, run_id: str, cwd: str) -> dict | None:
    raw = git_text(commit, "agent_log.json", cwd)
    if not raw:
        return None
    log = json.loads(raw)
    rows = log if isinstance(log, list) else [log]
    for row in reversed(rows):
        if run_id and row.get("run_id") == run_id:
            return row
        if row.get("date") and not run_id:
            return row
    return rows[-1] if rows else None


def compare_week(repo: str, day: str) -> dict:
    day_dir = Path(repo) / "shadow" / day
    ledger_path = Path(repo) / "shadow" / "ledger.jsonl"
    row = None
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("date") == day:
                row = item
    if row is None:
        raise FileNotFoundError(f"no ledger row for {day}")
    cursor_state = {
        "regime": json.loads((day_dir / "regime.json").read_text()),
        "research": json.loads((day_dir / "research.json").read_text()),
        "earnings": json.loads((day_dir / "earnings.json").read_text()),
        "devils_advocate": json.loads((day_dir / "devils_advocate.json").read_text()),
        "position_reviews": json.loads((day_dir / "position_reviews.json").read_text()),
    }
    live = _live_state(row["rebalance_commit"], row.get("live_run_id") or "", repo) or {}
    cursor_orders = _orders(json.loads((day_dir / "final.json").read_text()))
    live_orders = _orders(live.get("final_decisions") or [])
    cset = {(t, a) for t, a, _ in cursor_orders}
    lset = {(t, a) for t, a, _ in live_orders}
    cro_c = json.loads((day_dir / "cro.json").read_text()) if (day_dir / "cro.json").exists() else {}
    cro_l = live.get("cro") or {}
    parse_c = json.loads((day_dir / "parse_errors.json").read_text())
    report = {
        "date": day,
        "rebalance_commit": row["rebalance_commit"],
        "inputs_commit": row["inputs_commit"],
        "regime": {
            "cursor": (cursor_state.get("regime") or {}).get("regime"),
            "live": (live.get("regime") or {}).get("regime"),
        },
        "verdict_diffs": verdict_diffs(seat_verdicts(cursor_state), seat_verdicts(live)),
        "pm": {
            "cursor": _orders(json.loads((day_dir / "pm_proposal.json").read_text())),
            "live": _orders(live.get("portfolio_manager_proposed") or []),
        },
        "cro_vetoed": {
            "cursor": cro_c.get("rejected_tickers") or [],
            "live": cro_l.get("rejected_tickers") or [],
            "cursor_approved": cro_c.get("approved"),
            "live_approved": cro_l.get("approved"),
        },
        "final": {
            "cursor": cursor_orders,
            "live": live_orders,
            "overlap": sorted(cset & lset),
            "cursor_only": sorted(cset - lset),
            "live_only": sorted(lset - cset),
        },
        "parse_failures": {
            "cursor": row.get("parse_failures"),
            "cursor_detail": parse_c,
            "live_pm_parsed_ok": live.get("portfolio_manager_parsed_ok", True),
            "live_cro_api_failed": bool(cro_l.get("api_failed")),
        },
    }
    report["paper"] = _paper(repo, day, row)
    return report


def _paper(repo: str, day: str, row: dict) -> dict:
    inputs = json.loads((Path(repo) / "shadow" / day / "inputs.json").read_text())
    portfolio = inputs.get("portfolio") or {}
    prices = _prices_of(row["inputs_commit"], repo) or {}
    final = json.loads((Path(repo) / "shadow" / day / "final.json").read_text())
    start = float(portfolio.get("total_value") or 0)
    live_decisions = (_live_state(row["rebalance_commit"], row.get("live_run_id") or "", repo) or {}).get("final_decisions") or []
    out = {"start_value": start, "price_commit": row["inputs_commit"], "books": {}}
    for name, decisions in (("cursor", final), ("live", live_decisions)):
        cash, pos = apply_orders(portfolio, decisions, prices)
        value, missing = mark_to_market(cash, pos, prices)
        out["books"][name] = {
            "value_at_snapshot": value,
            "missing_prices": missing,
            "n_orders": len(_orders(decisions)),
        }
    spy0 = (prices.get("SPY") or {}).get("close")
    later = _later_snapshot(row["rebalance_commit"], repo)
    out["later_snapshot"] = later
    out["note"] = (
        "value_at_snapshot marks both books at the same pre-trade closes, so a "
        "rotation does not change value. forward_return uses the next snapshot's "
        "prices.close, which is that file's prior session, not an official print."
    )
    if later and spy0:
        later_px = _prices_of(later, repo) or {}
        spy1 = (later_px.get("SPY") or {}).get("close")
        out["spy_close_snapshot"] = spy0
        out["spy_close_later"] = spy1
        if spy1:
            out["spy_return"] = (float(spy1) / float(spy0)) - 1
        for name, decisions in (("cursor", final), ("live", live_decisions)):
            cash, pos = apply_orders(portfolio, decisions, prices)
            value, missing = mark_to_market(cash, pos, later_px)
            book = out["books"][name]
            book["value_later"] = value
            book["later_missing"] = missing
            if value is not None and start:
                book["forward_return"] = (value / start) - 1
    return out


def cumulative(repo: str) -> list[dict]:
    """Chain each week's forward return. One week is just that week."""
    path = Path(repo) / "shadow" / "ledger.jsonl"
    if not path.exists():
        return []
    rows = []
    growth = {"cursor": 1.0, "live": 1.0, "spy": 1.0}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        day = item["date"]
        try:
            report = compare_week(repo, day)
        except FileNotFoundError:
            continue
        paper = report.get("paper") or {}
        point = {"date": day}
        for name in ("cursor", "live"):
            ret = ((paper.get("books") or {}).get(name) or {}).get("forward_return")
            point[name] = ret
            if isinstance(ret, (int, float)):
                growth[name] *= 1 + ret
                point[f"{name}_cumulative"] = growth[name] - 1
        spy = paper.get("spy_return")
        point["spy"] = spy
        if isinstance(spy, (int, float)):
            growth["spy"] *= 1 + spy
            point["spy_cumulative"] = growth["spy"] - 1
        rows.append(point)
    return rows


def write_week(repo: str, day: str) -> Path:
    report = compare_week(repo, day)
    report["cumulative"] = cumulative(repo)
    dest = Path(repo) / "shadow" / day / "compare.json"
    dest.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare Cursor shadow weeks with live Claude")
    parser.add_argument("--date", default=None, help="One YYYY-MM-DD; default is every ledger row")
    parser.add_argument("--write", action="store_true", help="Write shadow/<date>/compare.json")
    args = parser.parse_args(argv)
    repo = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True).strip()
    days = [args.date] if args.date else []
    if not days:
        ledger = Path(repo) / "shadow" / "ledger.jsonl"
        if ledger.exists():
            for line in ledger.read_text().splitlines():
                if line.strip():
                    days.append(json.loads(line)["date"])
    if not days:
        print("no shadow ledger rows")
        return 0
    for day in days:
        report = compare_week(repo, day)
        if args.write:
            write_week(repo, day)
        regime = report["regime"]
        final = report["final"]
        print(f"{day} regime cursor={regime['cursor']} live={regime['live']}")
        print(f"  final overlap={final['overlap']} cursor_only={final['cursor_only']} live_only={final['live_only']}")
        print(f"  verdict diffs={len(report['verdict_diffs'])} parse cursor={report['parse_failures']['cursor']}")
        paper = report["paper"]
        for name, book in (paper.get("books") or {}).items():
            fwd = book.get("forward_return")
            if fwd is not None:
                print(f"  {name} forward {fwd:+.2%}")
        if paper.get("spy_return") is not None:
            print(f"  spy forward {paper['spy_return']:+.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
