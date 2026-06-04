"""
journal.py — Decision journal and portfolio kill-switch management.
"""

import json
import os
import uuid
from datetime import datetime


JOURNAL_FILE = "decision_journal.json"
PEAK_FILE = "portfolio_peak.json"
KILL_DRAWDOWN_THRESHOLD = 0.20


def _load(path: str, default):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return default


def _save(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


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
    trade_id = str(uuid.uuid4())[:8]
    journal.append({
        "trade_id": trade_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
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


def get_recent_decisions(n: int = 20) -> list:
    return _load(JOURNAL_FILE, [])[-n:]


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
