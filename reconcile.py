"""
reconcile.py — automated position reconciliation for the crash-recovery state (A7).

The stamp-first idempotency protocol (execute.py / preflight_gate.py) writes
`execution_started_at` to pending_decisions.json BEFORE the first order and
`executed_at` only AFTER all orders complete. If the process dies in between,
the next preflight sees `execution_started_at` with no `executed_at` and stops —
correctly failing toward a missed trade. But it previously left a HUMAN to diff
broker positions by hand: position state was *unknown* until someone looked, the
single biggest hole in the "autonomous" claim (PAPER_DRAFT §3.6, §6.8).

This module closes that gap WITHOUT relaxing the fail-safe:

  build_reconciliation()  PURE diff (no network, fully testable): given the
                          pre-trade holdings, the intended decisions, and the
                          actual live holdings, classify each intended order as
                          FILLED / NOT_FILLED / UNKNOWN and the run overall as
                          none_filled / all_filled / manual_required. Also flags
                          drift on tickers we never intended to touch.

  reconcile_crash_state() ORCHESTRATOR: detect the crash state, fetch LIVE broker
                          positions (ground truth — bypasses the cached
                          mcp_portfolio.json), run the diff, write
                          reconciliation_report.json, and print a specific,
                          diff-driven alert. It NEVER auto-trades. By default it
                          does not even mutate pending_decisions.json — it only
                          reports. Safe remediation (stamp executed_at when every
                          order demonstrably landed; clear the claim when none
                          did) is applied ONLY with apply=True AND only in those
                          two unambiguous cases. Anything ambiguous → MANUAL.

Design stance: a duplicate order in a live account can be unrecoverable, so the
default is always to under-act. Auto-remediation is opt-in and provably safe;
everything else stops for a human with the exact diff in hand.
"""

import json
import os
import sys
from datetime import datetime, timezone

PENDING   = "pending_decisions.json"
AGENT_LOG = "agent_log.json"
REPORT    = "reconciliation_report.json"

# Fractional-share match tolerance (Robinhood supports fractional qty).
QTY_TOL = 1e-4

# Classifications
NO_CRASH        = "no_crash"            # nothing to reconcile
RECONCILED_NONE = "none_filled"         # no intended order landed → safe to clear claim
RECONCILED_ALL  = "all_filled"          # every intended order landed → safe to stamp executed_at
MANUAL_REQUIRED = "manual_required"     # partial / ambiguous / unexpected drift → human


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _qty_map(positions) -> dict:
    """[{symbol/ticker, qty}] → {SYMBOL: qty}. Accepts either key."""
    out: dict[str, float] = {}
    for p in positions or []:
        sym = (p.get("symbol") or p.get("ticker") or "").upper()
        if not sym:
            continue
        out[sym] = out.get(sym, 0.0) + float(p.get("qty") or 0)
    return out


def _close(a: float, b: float, tol: float = QTY_TOL) -> bool:
    return abs(a - b) <= tol + tol * max(abs(a), abs(b))


def build_reconciliation(pre_positions: list, decisions: list,
                         live_positions: list, tol: float = QTY_TOL) -> dict:
    """Pure diff of intended vs actual post-crash holdings. No I/O, no network.

    pre_positions  : holdings BEFORE the run (from agent_log portfolio_snapshot).
    decisions      : the intended orders (pending_decisions.decisions), each with
                     `ticker`, `action` (BUY/SELL), and `qty`.
    live_positions : actual broker holdings fetched NOW.

    Returns a structured report with per-ticker legs, an overall classification,
    and a recommended_action string. The classification is deliberately
    conservative: it is `all_filled`/`none_filled` only when EVERY intended order
    is unambiguously in that state and no unexpected drift exists; otherwise it is
    `manual_required`.
    """
    pre  = _qty_map(pre_positions)
    live = _qty_map(live_positions)

    # Expected post-trade qty per intended ticker = pre + BUY qty − SELL qty.
    intended_tickers, expected = set(), dict(pre)
    for d in decisions or []:
        action = str(d.get("action", "")).upper()
        ticker = (d.get("ticker") or "").upper()
        qty    = float(d.get("qty") or 0)
        if action not in ("BUY", "SELL") or not ticker:
            continue
        intended_tickers.add(ticker)
        delta = qty if action == "BUY" else -qty
        expected[ticker] = expected.get(ticker, 0.0) + delta

    legs, n_filled, n_not, n_unknown = [], 0, 0, 0
    for t in sorted(intended_tickers):
        pre_q, exp_q, live_q = pre.get(t, 0.0), expected.get(t, 0.0), live.get(t, 0.0)
        if _close(live_q, exp_q, tol) and not _close(exp_q, pre_q, tol):
            state = "FILLED"
            n_filled += 1
        elif _close(live_q, pre_q, tol) and not _close(exp_q, pre_q, tol):
            state = "NOT_FILLED"
            n_not += 1
        elif _close(live_q, exp_q, tol) and _close(exp_q, pre_q, tol):
            # expected == pre (e.g. a zero-qty no-op); live matches both — benign.
            state = "FILLED"
            n_filled += 1
        else:
            state = "UNKNOWN"
            n_unknown += 1
        legs.append({"ticker": t, "pre_qty": round(pre_q, 6),
                     "expected_qty": round(exp_q, 6), "live_qty": round(live_q, 6),
                     "state": state})

    # Drift on tickers we never intended to touch — a position appearing or
    # changing outside the plan is a red flag (e.g. a stray order, or wrong account).
    unexpected = []
    for t in sorted(set(pre) | set(live)):
        if t in intended_tickers:
            continue
        if not _close(pre.get(t, 0.0), live.get(t, 0.0), tol):
            unexpected.append({"ticker": t, "pre_qty": round(pre.get(t, 0.0), 6),
                               "live_qty": round(live.get(t, 0.0), 6)})

    total = len(intended_tickers)
    if unexpected or n_unknown:
        cls = MANUAL_REQUIRED
    elif total and n_filled == total:
        cls = RECONCILED_ALL
    elif total and n_not == total:
        cls = RECONCILED_NONE
    elif total == 0:
        # Crash claim but no actionable orders — nothing could have filled.
        cls = RECONCILED_NONE
    else:
        cls = MANUAL_REQUIRED

    rec = {
        RECONCILED_ALL:  ("Every intended order is reflected in live positions. The "
                          "run completed but crashed before stamping executed_at. "
                          "SAFE to stamp executed_at and treat today as DONE."),
        RECONCILED_NONE: ("No intended order reached the broker (live == pre-trade). "
                          "SAFE to clear the stale execution claim; the day can be "
                          "re-attempted on fresh data."),
        MANUAL_REQUIRED: ("Partial fills, an unrecognized position state, or "
                          "unexpected drift. DO NOT auto-remediate. A human must "
                          "reconcile the legs below against the broker before any "
                          "further trading."),
    }[cls]

    return {
        "classification":     cls,
        "recommended_action": rec,
        "counts": {"intended": total, "filled": n_filled,
                   "not_filled": n_not, "unknown": n_unknown},
        "legs":               legs,
        "unexpected_changes": unexpected,
    }


def _fetch_live_positions() -> list:
    """Ground-truth holdings from the broker NOW — bypasses cached mcp_portfolio.json."""
    import execute
    execute._login()
    import robin_stocks.robinhood as rh
    holdings = rh.account.build_holdings(account_number=execute.AGENTIC_ACCOUNT) or {}
    return [{"symbol": t, "qty": float(d.get("quantity", 0) or 0)} for t, d in holdings.items()]


def _pre_positions_for_run(run_id: str, agent_log_path: str = AGENT_LOG) -> list:
    """Pre-trade holdings for `run_id` from agent_log.json's portfolio_snapshot
    (Step 1 captures the portfolio BEFORE any order)."""
    log = _read_json(agent_log_path)
    if not isinstance(log, list):
        return []
    for run in reversed(log):                      # most recent first
        if run.get("run_id") == run_id:
            return (run.get("portfolio_snapshot") or {}).get("positions", []) or []
    return []


def reconcile_crash_state(apply: bool = False, pending_path: str = PENDING,
                          live_positions: list | None = None) -> dict:
    """Detect the execution_started_at / no executed_at crash state and reconcile.

    Returns the report dict (always includes `classification`). Writes
    reconciliation_report.json and prints a specific alert. NEVER places trades.
    With apply=True, performs the safe remediation ONLY for the two unambiguous
    classifications (stamp executed_at / clear the claim); otherwise leaves
    pending_decisions.json untouched for a human.

    `live_positions` may be injected (tests / dry analysis); otherwise it is
    fetched live from the broker.
    """
    pending = _read_json(pending_path)
    crashed = bool(pending and pending.get("execution_started_at")
                   and not pending.get("executed_at"))
    if not crashed:
        return {"classification": NO_CRASH,
                "recommended_action": "No crash state detected; nothing to reconcile."}

    run_id = pending.get("run_id", "")
    if live_positions is None:
        try:
            live_positions = _fetch_live_positions()
        except Exception as e:                     # broker unreachable → fail safe
            report = {"classification": MANUAL_REQUIRED,
                      "recommended_action": (f"Could not fetch live broker positions "
                                             f"({str(e)[:160]}). Cannot reconcile "
                                             "automatically — MANUAL review required. "
                                             "DO NOT clear the execution claim."),
                      "run_id": run_id, "error": str(e)[:300]}
            _write_and_print(report, pending)
            return report

    pre = _pre_positions_for_run(run_id)
    report = build_reconciliation(pre, pending.get("decisions", []), live_positions)
    report["run_id"]          = run_id
    report["date"]            = pending.get("date")
    report["pre_source"]      = "agent_log.portfolio_snapshot" if pre else "MISSING"
    report["generated_at"]    = datetime.now(timezone.utc).isoformat()
    report["applied"]         = False

    if not pre and report["classification"] != RECONCILED_NONE:
        # No pre-trade baseline to diff against → cannot prove anything safely.
        report["classification"] = MANUAL_REQUIRED
        report["recommended_action"] = ("No pre-trade portfolio snapshot found for "
                                        f"run {run_id}; cannot prove fill state. "
                                        "MANUAL reconciliation required.")

    if apply:
        report["applied"] = _apply_safe_remediation(report, pending, pending_path)

    _record_health(report)
    _write_and_print(report, pending)
    return report


def _record_health(report: dict) -> None:
    """Surface the reconciliation result to system_health.json so the existing
    alert workflow (alert.yml) can open/append an issue. MANUAL_REQUIRED → FAILED
    (pages a human); the auto-resolvable cases → DEGRADED (informational). The
    routine must commit+push system_health.json after a crash-state preflight for
    the alert to fire — see DEPLOYMENT.md. Best-effort; never raises."""
    cls = report.get("classification")
    if cls in (NO_CRASH, None):
        return
    try:
        from health import append_check, FAILED, DEGRADED
        status = FAILED if cls == MANUAL_REQUIRED else DEGRADED
        append_check("crash_reconciliation", status,
                     message=report.get("recommended_action", "")[:300],
                     classification=cls, counts=report.get("counts"),
                     run_id=report.get("run_id"))
    except Exception:
        pass


def _apply_safe_remediation(report: dict, pending: dict, pending_path: str) -> bool:
    """Mutate pending_decisions.json ONLY in the two provably-safe cases. Returns
    whether a change was written. Logs every action."""
    cls = report["classification"]
    now = datetime.now(timezone.utc).isoformat()
    if cls == RECONCILED_ALL:
        pending["executed_at"] = now
        pending["reconciled"]  = {"by": "reconcile.py", "at": now, "result": cls}
        print(f"   ✅ APPLY: every order landed — stamping executed_at={now}; today is DONE.")
    elif cls == RECONCILED_NONE:
        pending["execution_started_at"] = None
        pending["reconciled"] = {"by": "reconcile.py", "at": now, "result": cls,
                                 "note": "no fills; stale claim cleared"}
        print("   ✅ APPLY: no orders landed — cleared stale execution_started_at; "
              "day may be re-attempted.")
    else:
        print("   ⛔ APPLY skipped: classification is MANUAL_REQUIRED — no automatic "
              "change is safe. Human reconciliation required.")
        return False
    with open(pending_path, "w") as f:
        json.dump(pending, f, indent=2)
    return True


def _write_and_print(report: dict, pending: dict | None) -> None:
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print("\n" + "=" * 64)
    print("🛠  CRASH-STATE RECONCILIATION")
    print("=" * 64)
    print(f"   run_id:        {report.get('run_id', '?')}")
    print(f"   classification: {report['classification'].upper()}")
    for leg in report.get("legs", []):
        print(f"     {leg['ticker']:<6} pre={leg['pre_qty']:<10} "
              f"expected={leg['expected_qty']:<10} live={leg['live_qty']:<10} {leg['state']}")
    for u in report.get("unexpected_changes", []):
        print(f"   ⚠ UNEXPECTED {u['ticker']}: pre={u['pre_qty']} live={u['live_qty']}")
    print(f"\n   → {report['recommended_action']}")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    rep = reconcile_crash_state(apply=apply)
    # Exit non-zero when a human must act, so a workflow step can branch/alert.
    sys.exit(0 if rep["classification"] in (NO_CRASH, RECONCILED_ALL, RECONCILED_NONE) else 2)
