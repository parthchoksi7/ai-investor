"""
journal.py — Decision journal and portfolio kill-switch management.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


JOURNAL_FILE = "decision_journal.json"
PEAK_FILE = "portfolio_peak.json"
AGENT_LOG_FILE = "agent_log.json"
TRANSACTIONS_FILE = "transactions.json"
PENDING_FILE = "pending_decisions.json"
# Durable once-per-ISO-week rebalance stamp (Phase 5, §6.5). pending_decisions.json
# alone cannot carry the week lock: the daily risk_watch envelope OVERWRITES it the
# next morning, which would erase Wednesday's executed_at and let a Thu/Fri catch-up
# re-run the whole rebalance (the §6.5 double-execution vector, cross-day variant).
# So the claim/executed stamps mirror rebalance-mode runs here; the preflight gate
# reads BOTH (pending first, this as the durable fallback). Committed to main by the
# routine like every other envelope artifact.
LAST_REBALANCE_FILE = "last_rebalance.json"
KILL_DRAWDOWN_THRESHOLD = 0.20


def _load(path: str, default):
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return default
    return default


def _load_list(path: str) -> list:
    """Load a JSON file that must be a list; coerce any other shape to []."""
    data = _load(path, [])
    return data if isinstance(data, list) else []


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX and Windows


def record_trade(
    ticker: str,
    action: str,
    target_weight: float,
    thesis: str,
    anti_thesis: str,
    catalysts: list,
    confidence: float,
    expected_return: float,
    invalidates_if: list,
    run_id: str = "",
) -> str:
    """Append a trade decision to the journal. Returns the generated trade_id.

    run_id is the reconciliation key: mark_transactions_live flips this run's
    entries to status="rejected" when the broker did not accept the order.
    """
    journal = _load_list(JOURNAL_FILE)
    trade_id = str(uuid.uuid4())
    journal.append({
        "trade_id": trade_id,
        "run_id": run_id,
        "date": datetime.now(_ET).strftime("%Y-%m-%d"),
        "ticker": ticker,
        "action": action,
        "target_weight": target_weight,
        "thesis": thesis,
        "anti_thesis": anti_thesis,
        "catalysts": catalysts,
        "confidence": confidence,
        "expected_return": expected_return,
        "invalidates_if": invalidates_if,
        "status": "open",
        "actual_return": None,
        "thesis_correct": None,
    })
    _save(JOURNAL_FILE, journal)
    return trade_id


def close_position(
    ticker: str,
    exit_price: float,
    avg_price: float,
    full_exit: bool,
    run_id: str = "",
) -> str | None:
    """Record a realized outcome on the most recent OPEN buy entry for `ticker`.

    Closes the feedback loop: decision_journal entries are created with
    actual_return=None / thesis_correct=None and nothing ever populated them, so
    a BUY thesis stayed "open" forever even after the position was sold. On a
    SELL this annotates the matching open BUY with its realized return and
    whether the thesis was directionally correct.

    Full exit  → status='closed'. Partial reduce → stays 'open', notes the trim.
    Realized return is per-share vs cost basis, so it is lot-size independent and
    correct for partial exits. Returns the trade_id touched, or None if there was
    no open BUY entry to close (a no-op — e.g. a SELL of a manually-acquired
    position, or a re-run after the entry already closed).

    NOTE (cloud caveat): main.py runs DRY_RUN in the cloud, so this is called on
    the speculative executed-decision list before broker reconciliation. If a
    SELL is subsequently rejected by the broker, the BUY here was closed
    prematurely and reconciliation (_reconcile_journal) does NOT re-open a
    'closed' entry. SELLs are placed first and rarely rejected, and this is
    feedback-quality data (not a capital control), so the exposure is accepted
    and documented rather than gated behind full fill reconciliation.
    """
    if not avg_price:
        return None  # no cost basis → cannot compute a return (avoid div-by-zero)
    journal = _load_list(JOURNAL_FILE)
    realized = round((exit_price - avg_price) / avg_price, 4)  # e.g. -0.064 = -6.4%

    # newest OPEN buy for this ticker
    target = None
    for entry in reversed(journal):
        if (entry.get("ticker") == ticker and entry.get("action") == "BUY"
                and entry.get("status") == "open"):
            target = entry
            break
    if target is None:
        return None

    target["actual_return"] = realized
    expected = target.get("expected_return") or 0
    # Thesis judged correct if direction matched; when an explicit expected
    # return was set, require realized to clear at least half of it.
    target["thesis_correct"] = (
        bool(realized > 0) if not expected else bool(realized >= expected * 0.5))
    target.setdefault("exits", []).append({
        "date": datetime.now(_ET).strftime("%Y-%m-%d"),
        "run_id": run_id,
        "exit_price": round(exit_price, 4),
        "avg_price": round(avg_price, 4),
        "realized_return": realized,
        "full_exit": full_exit,
    })
    if full_exit:
        target["status"] = "closed"
    _save(JOURNAL_FILE, journal)
    return target.get("trade_id")


_AGENT_LOG_MAX = 90  # ~3 months of trading days

def record_run(run_id: str, pipeline_state: dict) -> None:
    """Append a full agent pipeline run to agent_log.json (every run, including no-trade days)."""
    log = _load_list(AGENT_LOG_FILE)
    log.append({"run_id": run_id, **pipeline_state})
    if len(log) > _AGENT_LOG_MAX:
        log = log[-_AGENT_LOG_MAX:]
    _save(AGENT_LOG_FILE, log)


def record_transaction(tx: dict) -> None:
    """Append a detailed executed transaction to transactions.json."""
    txs = _load_list(TRANSACTIONS_FILE)
    txs.append(tx)
    _save(TRANSACTIONS_FILE, txs)


def _reconcile_trade_log(run_id: str, fills: dict) -> int:
    """Rewrite trades.csv rows for run_id against broker fills (atomic).

    Filled tickers get dry_run="False" + broker_order_id (+ fill price);
    unfilled tickers get dry_run="True" + empty broker_order_id. Rows with an
    empty/different run_id (older schema, manual entries) are never touched.
    Returns the number of rows rewritten.
    """
    import csv
    import execute  # lazy: attribute lookups stay monkeypatch-/test-friendly

    execute._migrate_trade_log()  # ensure the run_id column exists
    if not run_id or not os.path.isfile(execute.TRADE_LOG):
        return 0
    with open(execute.TRADE_LOG, newline="") as f:
        rows = list(csv.DictReader(f))

    touched = 0
    for row in rows:
        if row.get("run_id") != run_id:
            continue
        ticker = row.get("ticker")
        if ticker in fills:
            fill = fills[ticker] or {}
            row["dry_run"] = "False"
            if fill.get("order_id"):
                row["broker_order_id"] = fill["order_id"]
            if fill.get("price"):
                row["price"] = f"{float(fill['price']):.4f}"
                if row.get("qty"):
                    notional = float(row["qty"]) * float(fill["price"])
                    row["total_value"] = f"{notional:.2f}"
                    # Keep the paper-shadow twin in lockstep with the live fill.
                    row["total_value_100x"] = f"{notional * execute.SHADOW_MULTIPLIER:.2f}"
        else:
            row["dry_run"] = "True"
            row["broker_order_id"] = ""
        touched += 1

    if touched:
        tmp = execute.TRADE_LOG + ".tmp"
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=execute.TRADE_LOG_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in execute.TRADE_LOG_FIELDS})
        os.replace(tmp, execute.TRADE_LOG)
    return touched


def _reconcile_journal(run_id: str, fills: dict) -> int:
    """Set decision_journal status for run_id entries from broker fills.

    Unfilled → "rejected" (prior_journal/consumers only read status=="open").
    Filled but previously marked "rejected" (e.g. a re-reconcile with the real
    fills.json after an empty first pass) → restored to "open". Statuses other
    than open/rejected (closed, …) are never touched. Returns entries changed.
    """
    if not run_id:
        return 0
    journal = _load_list(JOURNAL_FILE)
    changed = 0
    for entry in journal:
        if entry.get("run_id") != run_id:
            continue
        filled = entry.get("ticker") in fills
        if not filled and entry.get("status") == "open":
            entry["status"] = "rejected"
            changed += 1
        elif filled and entry.get("status") == "rejected":
            entry["status"] = "open"
            changed += 1
    if changed:
        _save(JOURNAL_FILE, journal)
    return changed


def mark_transactions_live(run_id: str, fills: dict | None = None,
                           force_flip_all: bool = False) -> None:
    """Reconcile ALL of a run's speculative logs against actual broker fills.

    main.py runs DRY_RUN=true in the cloud (robin_stocks is blocked), so it
    writes every decision to transactions.json, trades.csv, and
    decision_journal.json as speculative (dry_run=True / status="open") BEFORE
    the real MCP orders are placed in STEP 4. publish.py filters dry_run
    records and get_trade_history feeds only non-dry-run rows to the agents,
    so nothing is published or fed back until reconciled here.

    `fills` maps ticker -> {"order_id": str, "price": float|None} for orders
    the broker ACTUALLY accepted (returned an order id) — built by routine
    STEP 4 and written to fills.json. Reconciliation per log:
      transactions.json      — filled: dry_run=False + broker_order_id/price;
                               unfilled: stays dry_run=True
      trades.csv             — same, keyed by the run_id column
      decision_journal.json  — unfilled: status="rejected" (excluded from
                               prior_journal); filled: stays/returns "open"
    This preserves the fd9d56a invariant across every log: a live record
    exists ⟺ the broker accepted the order.

    `fills=None` raises — the legacy bare call flipped every transaction live
    with only a printed warning, silently re-creating the phantom-fill bug if
    an operator or a regressed routine prompt called it without fills.json.
    A true emergency flip-all (broker confirmed ALL orders manually, fills.json
    unrecoverable) requires the explicit force_flip_all=True.

    Failure-direction health check (dict mode): if this run had decisions but
    ZERO were reconciled live, records reconciliation=FAILED on
    system_health.json — orders may have been placed with fills.json
    missing/empty, which is exactly the state that must page a human.
    """
    if fills is None and not force_flip_all:
        raise ValueError(
            "mark_transactions_live requires broker fills (ticker -> {order_id, price}, "
            "from fills.json). Calling without fills cannot distinguish accepted orders "
            "from rejections and would publish phantom fills. If the broker confirmed "
            "ALL orders and fills.json is unrecoverable, pass force_flip_all=True."
        )

    txs = _load_list(TRANSACTIONS_FILE)
    run_txs = [tx for tx in txs if tx.get("run_id") == run_id]
    updated = 0
    for tx in run_txs:
        if not tx.get("dry_run"):
            continue
        ticker = tx.get("ticker")
        if force_flip_all:
            tx["dry_run"] = False
            updated += 1
        elif ticker in fills:
            tx["dry_run"] = False
            fill = fills[ticker] or {}
            if fill.get("order_id"):
                tx["broker_order_id"] = fill["order_id"]
            if fill.get("price"):
                tx["price"] = round(float(fill["price"]), 4)
                if tx.get("qty"):
                    tx["total_value"] = round(float(tx["qty"]) * float(fill["price"]), 2)
            updated += 1
        # else: ticker not in fills → rejected/unfilled → leave dry_run=True
    if updated:
        _save(TRANSACTIONS_FILE, txs)
        print(f"   ✅ Marked {updated} transaction(s) as live (run_id={run_id})")
    else:
        print(f"   ℹ️  No dry_run transactions found for run_id={run_id}")

    if force_flip_all:
        print("   ⚠️  EMERGENCY force_flip_all — every run transaction flipped live "
              "without broker fills. trades.csv/journal NOT reconciled. Verify in Robinhood.")
        return

    csv_rows  = _reconcile_trade_log(run_id, fills)
    jrnl_rows = _reconcile_journal(run_id, fills)
    print(f"   🔁 Reconciled {csv_rows} trades.csv row(s), {jrnl_rows} journal entr(ies) "
          f"against {len(fills)} fill(s)")

    # Failure-direction guard: decisions existed but nothing came back live.
    if run_txs:
        from health import append_check, FAILED, DEGRADED, OK
        unfilled = sorted({tx.get("ticker") for tx in run_txs} - set(fills))
        if not fills:
            append_check("reconciliation", FAILED,
                         message=f"{len(run_txs)} decision(s) for run {run_id} but ZERO fills "
                                 "reconciled — orders may have been placed with fills.json "
                                 "missing or empty. Verify against Robinhood order history.",
                         unfilled=unfilled)
        elif unfilled:
            append_check("reconciliation", DEGRADED,
                         message=f"{len(unfilled)} of {len(run_txs)} order(s) not accepted by "
                                 "broker — kept speculative (dry_run/rejected)",
                         unfilled=unfilled, filled=sorted(fills))
        else:
            append_check("reconciliation", OK, filled=sorted(fills))


def _mirror_rebalance_stamp(pending: dict) -> None:
    """Mirror a REBALANCE-mode envelope's claim/executed stamps into
    last_rebalance.json — the durable once-per-ISO-week lock (§6.5).

    Only rebalance runs are mirrored (a missing `mode` = a legacy/daily-era
    envelope = rebalance semantics). risk_watch envelopes never touch this file,
    so the week lock survives their daily overwrite of pending_decisions.json.
    `tickers` feeds the cross-mode SELL interlock: risk_watch must not SELL a
    name the rebalance already traded this ISO week (§6.5.3).

    A claim WITHOUT executed_at still counts as "rebalance attempted this week"
    — orders may exist (Scenario B), so a Thu/Fri catch-up must not re-run.
    """
    if pending.get("mode", "rebalance") != "rebalance":
        return
    from market_calendar import iso_week_of
    try:
        week = iso_week_of(pending.get("date"))
    except Exception:
        return  # undateable envelope — cannot key a week lock; gate falls back to pending
    _save(LAST_REBALANCE_FILE, {
        "iso_week":             week,
        "date":                 pending.get("date"),
        "run_id":               pending.get("run_id"),
        "execution_started_at": pending.get("execution_started_at"),
        "executed_at":          pending.get("executed_at"),
        "tickers": sorted({d.get("ticker") for d in pending.get("decisions", [])
                           if isinstance(d, dict) and d.get("ticker")
                           and str(d.get("action", "")).upper() in ("BUY", "SELL")}),
    })


def mark_execution_started(run_id: str) -> None:
    """Stamp pending_decisions.json BEFORE the first order is placed.

    Closes the cross-attempt double-fill window: if an attempt places some
    orders then crashes before executed_at is stamped and pushed, the next
    hourly attempt would otherwise see executed_at=null + fresh data and
    re-place everything. The cloud routine commits and pushes this claim
    BEFORE placing the first order, so the claim is durable across attempts.
    preflight_gate and the routine's guards treat a non-null
    execution_started_at dated today exactly like executed_at: STOP — recovery
    goes through the Scenario B runbook (position diff), never blind re-execution.

    Rebalance-mode claims are mirrored to last_rebalance.json so a Wednesday
    crash-mid-execution also disables the Thu/Fri catch-up (orders may exist).

    Fails toward missed trades, never duplicate trades.
    """
    if not os.path.isfile(PENDING_FILE):
        return
    with open(PENDING_FILE) as f:
        pending = json.load(f)
    if not isinstance(pending, dict):
        return  # old bare-list format — can't stamp
    if pending.get("run_id") != run_id:
        return  # stale file from a different run
    if pending.get("execution_started_at"):
        return  # already claimed — preserve the original claim timestamp
    pending["execution_started_at"] = datetime.now(timezone.utc).isoformat()
    _save(PENDING_FILE, pending)
    _mirror_rebalance_stamp(pending)
    print(f"   🚩 Execution claim set (run_id={run_id}) — placing orders next")


def mark_pending_executed(run_id: str) -> None:
    """Stamp pending_decisions.json as executed to prevent double-execution on retry.

    Rebalance-mode stamps are mirrored to last_rebalance.json — the durable
    once-per-ISO-week lock the gate's Thu/Fri catch-up reads (§6.5)."""
    if not os.path.isfile(PENDING_FILE):
        return
    with open(PENDING_FILE) as f:
        pending = json.load(f)
    if not isinstance(pending, dict):
        return  # old bare-list format — can't stamp
    if pending.get("run_id") != run_id:
        return  # stale file from a different run
    if pending.get("executed_at") is not None:
        return  # already stamped — preserve original execution timestamp
    pending["executed_at"] = datetime.now(timezone.utc).isoformat()
    _save(PENDING_FILE, pending)
    _mirror_rebalance_stamp(pending)
    print(f"   🔒 Execution lock set (run_id={run_id})")


def get_recent_decisions(n: int = 20) -> list:
    return _load_list(JOURNAL_FILE)[-n:]


def get_ticker_history(ticker: str, n: int = 3) -> list[dict]:
    """Most-recent journal entries for one ticker (open or closed), with outcomes.

    Feeds the Research Analyst a memory of how prior theses for this name played
    out — `actual_return` / `thesis_correct` are populated by close_position.
    """
    rows = [e for e in _load_list(JOURNAL_FILE) if e.get("ticker") == ticker]
    return rows[-n:]


def consecutive_cash_above(threshold: float) -> int:
    """Consecutive recent pipeline runs (reverse-chronological) where cash_pct > threshold.

    Reads agent_log.json which record_run() appends to each cycle. The current
    run's portfolio_snapshot is written to agent_log BEFORE this is called from
    main.py, so the count includes today and the streak is accurate in real-time.

    Returns 0 if agent_log is empty or cash is currently at/below the threshold.
    """
    count = 0
    for entry in reversed(_load_list(AGENT_LOG_FILE)):
        ps = entry.get("portfolio_snapshot", {})
        total = ps.get("total_value") or 0
        if not total:
            break
        if ps.get("cash", 0) / total * 100 > threshold:
            count += 1
        else:
            break
    return count


def recently_exited(within_days: int = 10) -> dict:
    """ticker -> most recent CLOSED entry whose last exit was within `within_days`.

    Surfaces the re-entry blind spot to the Portfolio Manager: a name the system
    just sold should not be silently rebought without justifying the reversal.
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=within_days)
    out: dict = {}
    for e in _load_list(JOURNAL_FILE):
        if e.get("status") != "closed":
            continue
        exits = e.get("exits") or []
        if not exits:
            continue
        try:
            d = date.fromisoformat(exits[-1]["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if d >= cutoff:
            out[e["ticker"]] = e  # last write wins = most recent in file order
    return out


def check_kill_switches(portfolio: dict) -> tuple[bool, str]:
    """
    Returns (kill_active, reason).
    Blocks new purchases when portfolio drawdown exceeds KILL_DRAWDOWN_THRESHOLD.
    """
    total = portfolio.get("total_value", 0)
    if total <= 0:
        return False, ""

    peak_data = _load(PEAK_FILE, {})
    peak = peak_data.get("peak", total)

    if total >= peak:
        _save(PEAK_FILE, {"peak": total, "updated": datetime.now(_ET).strftime("%Y-%m-%d")})
        return False, ""

    drawdown = (peak - total) / peak
    if drawdown >= KILL_DRAWDOWN_THRESHOLD:
        return True, (
            f"Portfolio drawdown {drawdown:.1%} exceeds {KILL_DRAWDOWN_THRESHOLD:.0%} threshold. "
            f"Peak: ${peak:,.2f} → Current: ${total:,.2f}. Manual review required before resuming."
        )

    return False, ""
