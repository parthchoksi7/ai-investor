"""
reconcile_fills.py — make the audit logs agree with the broker's actual fills.

Oct 3 2026 remediation 2.5. Two gaps made every after-tax / realized-P&L figure
an estimate that disagreed with Robinhood:

  1. transactions.json / trades.csv recorded the DECISION-time quote as `price`
     (the routine left the fill price null), not the broker's average fill price.
  2. The 12 initial-construction BUYs of 2026-06-08 never reached
     transactions.json (logging began 2026-06-10), so every later SELL of those
     names had no in-log cost basis — performance.py's 12 "uncovered" sells.

Input: an evidence file of broker fills (evidence/broker_fills_YYYY-MM-DD.json,
pulled from the Robinhood MCP and validated against the broker's own realized
P&L before use). Matching, per filled broker order:

  transactions.json  live tx with the same broker_order_id → price := fill
                     else live tx with same ET date / ticker / side / qty → also
                       stamp broker_order_id
                     else (no tx at all) → append a tx marked
                       source="broker_backfill" (dry_run=False)
  trades.csv         live row with same date / ticker / side / qty →
                     fill_price, price := fill, total_value recomputed,
                     broker_order_id filled (slippage_bps is NOT backfilled — see
                     reconcile_trade_log)

Rows are only ever corrected toward the broker; nothing is deleted. Speculative
rows (dry_run=True — unplaced orders) are never touched or flipped.

Usage:
    python reconcile_fills.py evidence/broker_fills_2026-10-03.json          # dry run
    python reconcile_fills.py evidence/broker_fills_2026-10-03.json --apply
"""

from __future__ import annotations

import csv
import json
import os
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
TRANSACTIONS_FILE = "transactions.json"
QTY_TOL = 1e-6


def _et_date(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(_ET).strftime("%Y-%m-%d")


def _load_orders(path: str) -> list[dict]:
    doc = json.load(open(path))
    out = []
    for o in doc.get("orders", []):
        out.append({
            "order_id": o["order_id"],
            "ticker":   o["symbol"].upper(),
            "action":   o["side"].upper(),
            "qty":      float(o["quantity"]),
            "price":    float(o["average_price"]),
            "date":     _et_date(o["created_at"]),
            "timestamp": o["created_at"],
        })
    return out


def _same(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= QTY_TOL


def reconcile_transactions(txs: list[dict], orders: list[dict]) -> dict:
    """Mutates `txs` in place. Returns a report dict."""
    report = {"repriced": 0, "id_stamped": 0, "backfilled": [], "unchanged": 0}
    used: set[int] = set()
    for o in orders:
        idx = next((i for i, t in enumerate(txs)
                    if not t.get("dry_run") and t.get("broker_order_id") == o["order_id"]), None)
        if idx is None:
            idx = next((i for i, t in enumerate(txs)
                        if i not in used and not t.get("dry_run")
                        and not t.get("broker_order_id")
                        and t.get("date") == o["date"]
                        and str(t.get("ticker", "")).upper() == o["ticker"]
                        and str(t.get("action", "")).upper() == o["action"]
                        and _same(t.get("qty") or 0, o["qty"])), None)
            if idx is not None:
                txs[idx]["broker_order_id"] = o["order_id"]
                report["id_stamped"] += 1
        if idx is None:
            txs.append({
                "transaction_id": str(uuid.uuid4()),
                "run_id":         f"broker-backfill-{o['date']}",
                "date":           o["date"],
                "timestamp":      o["timestamp"],
                "ticker":         o["ticker"],
                "action":         o["action"],
                "qty":            o["qty"],
                "price":          round(o["price"], 4),
                "total_value":    round(o["qty"] * o["price"], 2),
                "broker_order_id": o["order_id"],
                "dry_run":        False,
                "source":         "broker_backfill",
                "rationale":      "Backfilled from broker fills (reconcile_fills.py) — "
                                  "the order predates transaction logging.",
            })
            used.add(len(txs) - 1)
            report["backfilled"].append(f"{o['date']} {o['action']} {o['ticker']}")
            continue
        used.add(idx)
        t = txs[idx]
        if abs(float(t.get("price") or 0) - o["price"]) > 1e-4:
            t.setdefault("decision_price", t.get("price"))
            t["price"] = round(o["price"], 4)
            if t.get("qty"):
                t["total_value"] = round(float(t["qty"]) * o["price"], 2)
            report["repriced"] += 1
        else:
            report["unchanged"] += 1
    return report


def reconcile_trade_log(rows: list[dict], orders: list[dict]) -> dict:
    report = {"repriced": 0, "unmatched_orders": []}
    used: set[int] = set()
    for o in orders:
        idx = next((i for i, r in enumerate(rows)
                    if i not in used and r.get("dry_run") == "False"
                    and r.get("date") == o["date"]
                    and r.get("ticker", "").upper() == o["ticker"]
                    and r.get("action", "").upper() == o["action"]
                    and _same(r.get("qty") or 0, o["qty"])), None)
        if idx is None:
            report["unmatched_orders"].append(f"{o['date']} {o['action']} {o['ticker']}")
            continue
        used.add(idx)
        r = rows[idx]
        try:
            decision_px = float(r.get("price") or 0)
        except ValueError:
            decision_px = 0.0
        r["fill_price"] = f"{o['price']:.4f}"
        # slippage_bps is deliberately NOT backfilled: for rows reconciled since
        # Jun 12, `price` may already hold a routine-reported fill (or a market
        # order's collar price), so fill-vs-`price` is not decision slippage and
        # would corrupt the A5 dataset. Going forward, mark_transactions_live
        # computes it at reconcile time, while `price` is still the decision quote.
        if not r.get("broker_order_id"):
            r["broker_order_id"] = o["order_id"]
        if abs(decision_px - o["price"]) > 1e-4:
            r["price"] = f"{o['price']:.4f}"
            try:
                import execute
                notional = float(r["qty"]) * o["price"]
                r["total_value"] = f"{notional:.2f}"
                if "total_value_100x" in r:
                    r["total_value_100x"] = f"{notional * execute.SHADOW_MULTIPLIER:.2f}"
            except Exception:
                pass
            report["repriced"] += 1
    return report


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    apply = "--apply" in argv
    orders = _load_orders(argv[0])

    import execute
    import performance
    txs = json.load(open(TRANSACTIONS_FILE)) if os.path.isfile(TRANSACTIONS_FILE) else []
    before_lots, before_uncovered = performance.compute_realized_lots(txs)
    before = performance.realized_summary(before_lots)

    tx_report = reconcile_transactions(txs, orders)
    after_lots, after_uncovered = performance.compute_realized_lots(txs)
    after = performance.realized_summary(after_lots)

    rows: list[dict] = []
    if os.path.isfile(execute.TRADE_LOG):
        with open(execute.TRADE_LOG, newline="") as f:
            rows = list(csv.DictReader(f))
    csv_report = reconcile_trade_log(rows, orders)

    print(f"Broker fills: {len(orders)}")
    print(f"transactions.json: repriced {tx_report['repriced']}, id-stamped {tx_report['id_stamped']}, "
          f"unchanged {tx_report['unchanged']}, backfilled {len(tx_report['backfilled'])}")
    for b in tx_report["backfilled"]:
        print(f"   + {b}")
    print(f"trades.csv: repriced {csv_report['repriced']}; broker orders with no live row: "
          f"{csv_report['unmatched_orders'] or 'none'}")
    key = next((k for k in ("realized_gain_pretax", "realized_gain", "realized") if k in after), None)
    print(f"Uncovered SELLs: {len(before_uncovered)} → {len(after_uncovered)}")
    if key:
        print(f"Realized gain (log): {before.get(key)} → {after.get(key)}")
    print("Broker-reported realized P&L for comparison: see the evidence file "
          f"(broker_realized_pnl_total={json.load(open(argv[0])).get('broker_realized_pnl_total')})")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0
    tmp = TRANSACTIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(txs, f, indent=2)
    os.replace(tmp, TRANSACTIONS_FILE)
    if rows:
        tmp = execute.TRADE_LOG + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=execute.TRADE_LOG_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in execute.TRADE_LOG_FIELDS})
        os.replace(tmp, execute.TRADE_LOG)
    print("\nAPPLIED — transactions.json and trades.csv reconciled to broker fills.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
