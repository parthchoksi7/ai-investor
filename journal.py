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
KILL_DRAWDOWN_THRESHOLD = 0.20


def _load(path: str, default):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
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
                    row["total_value"] = f"{float(row['qty']) * float(fill['price']):.2f}"
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
    print(f"   🚩 Execution claim set (run_id={run_id}) — placing orders next")


def mark_pending_executed(run_id: str) -> None:
    """Stamp pending_decisions.json as executed to prevent double-execution on retry."""
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
    print(f"   🔒 Execution lock set (run_id={run_id})")


def get_recent_decisions(n: int = 20) -> list:
    return _load_list(JOURNAL_FILE)[-n:]


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
        _save(PEAK_FILE, {"peak": total, "updated": datetime.now().strftime("%Y-%m-%d")})
        return False, ""

    drawdown = (peak - total) / peak
    if drawdown >= KILL_DRAWDOWN_THRESHOLD:
        return True, (
            f"Portfolio drawdown {drawdown:.1%} exceeds {KILL_DRAWDOWN_THRESHOLD:.0%} threshold. "
            f"Peak: ${peak:,.2f} → Current: ${total:,.2f}. Manual review required before resuming."
        )

    return False, ""
