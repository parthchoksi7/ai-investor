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
) -> str:
    """Append a trade decision to the journal. Returns the generated trade_id."""
    journal = _load(JOURNAL_FILE, [])
    trade_id = str(uuid.uuid4())
    journal.append({
        "trade_id": trade_id,
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
    log = _load(AGENT_LOG_FILE, [])
    log.append({"run_id": run_id, **pipeline_state})
    if len(log) > _AGENT_LOG_MAX:
        log = log[-_AGENT_LOG_MAX:]
    _save(AGENT_LOG_FILE, log)


def record_transaction(tx: dict) -> None:
    """Append a detailed executed transaction to transactions.json."""
    txs = _load(TRANSACTIONS_FILE, [])
    txs.append(tx)
    _save(TRANSACTIONS_FILE, txs)


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
    data = _load(JOURNAL_FILE, [])
    if not isinstance(data, list):
        data = []
    return data[-n:]


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
