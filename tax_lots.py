"""
tax_lots.py — read-only tax-lot accounting (#6).

Reconstructs OPEN tax lots (qty, cost basis, acquired date) for each ticker from
transactions.json via FIFO — the same matching performance.compute_realized_lots
uses for realized gains. **Read-only**: it derives lots on demand and persists
nothing, so it stays entirely out of the money/state path (no second write to
reconcile, no double-fill risk). Used by the net-edge gate and any future
HIFO / holding-period logic.

(The FIFO consume loop is intentionally a small local copy rather than a shared
import — performance.compute_realized_lots returns realized lots and discards the
open remainder, which is exactly what we need here, so the two are duals of the
same matching. A future refactor could unify them.)
"""

from collections import defaultdict, deque
from datetime import date


def open_lots(transactions: list, ticker: str | None = None):
    """Open tax lots after FIFO-consuming all SELLs.

    Returns {ticker: [{qty, cost_basis, acquired}]} (most-recent-last per ticker),
    or just that ticker's list when `ticker` is given. dry_run rows are excluded;
    rows are processed in timestamp order so a SELL only consumes prior BUYs.
    """
    txs = sorted((t for t in transactions if not t.get("dry_run")),
                 key=lambda t: (t.get("timestamp") or t.get("date") or ""))
    lots: dict[str, deque] = defaultdict(deque)
    for t in txs:
        action = str(t.get("action", "")).upper()
        tk     = t.get("ticker", "")
        qty    = float(t.get("qty") or 0)
        price  = float(t.get("price") or 0)
        acq    = t.get("date") or (t.get("timestamp") or "")[:10]
        if qty <= 0:
            continue
        if action == "BUY":
            lots[tk].append([qty, price, acq])
        elif action == "SELL":
            remaining = qty
            while remaining > 1e-9 and lots[tk]:
                lot  = lots[tk][0]
                take = min(remaining, lot[0])
                lot[0]    -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots[tk].popleft()

    out = {tk: [{"qty": round(q, 6), "cost_basis": p, "acquired": acq} for q, p, acq in v]
           for tk, v in lots.items() if v}
    return out.get(ticker, []) if ticker is not None else out


def holding_days(acquired: str, today: str | None = None) -> int | None:
    """Calendar days held since `acquired` (YYYY-MM-DD). None if unparseable."""
    try:
        a = date.fromisoformat(str(acquired)[:10])
        t = date.fromisoformat(today[:10]) if today else date.today()
    except (ValueError, TypeError):
        return None
    return (t - a).days
