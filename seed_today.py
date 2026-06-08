"""
seed_today.py — One-off script to backfill today's (2026-06-08) trades into Supabase.
Data sourced directly from Robinhood MCP orders + pending_decisions.json in git.
Safe to run multiple times (upsert by primary key).
"""

import os
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

TODAY           = "2026-06-08"
RUN_ID          = "20260608-145656"
REGIME          = "NEUTRAL"
TOTAL_VALUE     = 496.43
CASH            = 79.63
STARTING_CAPITAL = 500.0

# ── Rationales from pending_decisions.json (committed to git) ─────────────────
RATIONALES = {
    "AAPL": "Highest composite score (73.5) with strong momentum (95.9) and relatively low volatility (22.2%) provides best risk-adjusted entry in neutral regime.",
    "EQIX": "Strong composite score with lowest volatility among top candidates (17.1%) and low beta (0.49) offers defensive growth exposure in uncertain regime.",
    "MS":   "Near-perfect momentum score (99.4) and negative beta (-0.14) provides diversification and momentum exposure with minimal market correlation.",
    "LIN":  "Negative beta (-0.08) and low volatility (18.8%) with solid composite score (69.6) offers defensive diversification anchor for the portfolio.",
    "JNJ":  "Lowest beta (0.1) among candidates with low volatility (17.4%) provides defensive ballast and sector diversification into healthcare.",
    "BAC":  "Solid composite score (69.3) with low volatility (21.7%) adds financials sector exposure at attractive risk-adjusted profile alongside MS.",
    "MRK":  "Strong momentum (88.2) with low beta (0.38) and moderate volatility (26.9%) adds healthcare diversification distinct from JNJ.",
    "TJX":  "Low beta (0.48) and moderate volatility (22.8%) with solid composite score provides defensive consumer discretionary exposure.",
    "PLD":  "Low volatility (20.6%) and moderate beta (0.75) adds real estate sector diversification with solid composite score (67.9).",
    "EOG":  "Negative beta (-0.92) provides strong portfolio hedge and energy sector diversification with solid momentum (91.0).",
    "ABBV": "Low beta (0.28) and moderate volatility (25.6%) adds additional healthcare/pharma diversification with defensive characteristics.",
    "JPM":  "Low volatility (21.7%) relative to financials peers with solid composite score adds large-cap financial breadth to complement BAC and MS.",
}

TARGET_WEIGHTS = {
    "AAPL": 0.08, "EQIX": 0.08, "MS": 0.07, "LIN": 0.07, "JNJ": 0.07,
    "BAC": 0.07,  "MRK": 0.07,  "TJX": 0.07, "PLD": 0.07, "EOG": 0.07,
    "ABBV": 0.06, "JPM": 0.06,
}

# ── Orders from Robinhood MCP ─────────────────────────────────────────────────
# id, symbol, qty, fill_price, timestamp
ORDERS = [
    ("6a26da76-e1ba-46f1-a0fa-f249d23fae6c", "AAPL", 0.127061, 314.9199, "2026-06-08T15:06:30.418Z"),
    ("6a26dac7-31b8-471f-8963-a57a8ce2f461", "EQIX", 0.037203, 1076.7399, "2026-06-08T15:07:51.684Z"),
    ("6a26dac9-0be0-41d3-b76a-217230b3afba", "MS",   0.162508, 215.4399, "2026-06-08T15:07:53.941Z"),
    ("6a26dafa-fce8-4de1-8bd8-1b1687abd642", "BAC",  0.646234, 54.2199, "2026-06-08T15:08:42.345Z"),
    ("6a26dafa-6e1d-44fb-93a4-938563b5dffb", "MRK",  0.289350, 121.08, "2026-06-08T15:08:42.903Z"),
    ("6a26dafb-be05-4782-92f7-93238a7e0dd8", "TJX",  0.216928, 161.48, "2026-06-08T15:08:43.549Z"),
    ("6a26dafb-9acf-4e83-8c0e-70afe3d380b0", "PLD",  0.245970, 142.4499, "2026-06-08T15:08:44.16Z"),
    ("6a26dafc-f152-4de4-a28c-dd845162d2b5", "EOG",  0.247912, 141.1199, "2026-06-08T15:08:44.867Z"),
    ("6a26dafd-33d7-4a35-b2df-2b4fca3c3993", "ABBV", 0.133894, 224.4299, "2026-06-08T15:08:45.411Z"),
    ("6a26dafe-deb3-410c-9572-97e571a07e89", "JPM",  0.095754, 313.7499, "2026-06-08T15:08:46.234Z"),
    ("6a26dbaa-b198-4a38-8883-1e09ae9f1980", "LIN",  0.069516, 503.88, "2026-06-08T15:11:38.508Z"),
    ("6a26dbab-3e03-46bc-99d0-514d9b5e6c75", "JNJ",  0.150539, 232.77, "2026-06-08T15:11:39.442Z"),
]

# ── Positions from Robinhood MCP ──────────────────────────────────────────────
# symbol, qty, avg_buy_price
POSITIONS_RAW = [
    ("AAPL", 0.127061, 314.890000),
    ("EQIX", 0.037203, 1076.790000),
    ("MS",   0.162508, 215.440000),
    ("BAC",  0.646234, 54.220000),
    ("MRK",  0.289350, 121.060000),
    ("TJX",  0.216928, 161.480000),
    ("PLD",  0.245970, 142.460000),
    ("EOG",  0.247912, 141.140000),
    ("ABBV", 0.133894, 224.430000),
    ("JPM",  0.095754, 313.720000),
    ("LIN",  0.069516, 503.910000),
    ("JNJ",  0.150539, 232.760000),
]


def seed():
    print("Seeding Supabase with 2026-06-08 data...")

    # ── 1. Portfolio snapshot ─────────────────────────────────────────────────
    cumulative_return = round((TOTAL_VALUE - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 4)
    snapshot = {
        "date":                  TODAY,
        "total_value":           TOTAL_VALUE,
        "cash":                  CASH,
        "num_positions":         len(POSITIONS_RAW),
        "cumulative_return_pct": cumulative_return,
        "drawdown_pct":          round(max(0.0, (STARTING_CAPITAL - TOTAL_VALUE) / STARTING_CAPITAL * 100), 4),
        "regime":                REGIME,
        "run_id":                RUN_ID,
    }
    client.table("portfolio_snapshots").upsert(snapshot).execute()
    print(f"  ✓ portfolio_snapshots: value=${TOTAL_VALUE} return={cumulative_return:+.2f}%")

    # ── 2. Positions ──────────────────────────────────────────────────────────
    client.table("positions").delete().neq("ticker", "___never___").execute()
    pos_rows = []
    for symbol, qty, avg_cost in POSITIONS_RAW:
        fill_price = next(o[3] for o in ORDERS if o[1] == symbol)
        market_value = round(qty * avg_cost, 4)
        weight_pct   = round(market_value / TOTAL_VALUE * 100, 4)
        unrealized_pct = round((fill_price - avg_cost) / avg_cost * 100, 4) if avg_cost > 0 else 0.0
        pos_rows.append({
            "ticker":         symbol,
            "weight_pct":     weight_pct,
            "quantity":       qty,
            "avg_cost":       avg_cost,
            "current_price":  fill_price,
            "unrealized_pct": unrealized_pct,
            "entry_date":     TODAY,
        })
    client.table("positions").insert(pos_rows).execute()
    print(f"  ✓ positions: {len(pos_rows)} holdings")

    # ── 3. Trades ─────────────────────────────────────────────────────────────
    trade_rows = []
    for order_id, symbol, qty, fill_price, ts in ORDERS:
        trade_rows.append({
            "id":            order_id,
            "date":          TODAY,
            "ticker":        symbol,
            "action":        "BUY",
            "qty":           qty,
            "price":         fill_price,
            "total_value":   round(qty * fill_price, 4),
            "target_weight": TARGET_WEIGHTS.get(symbol),
            "regime":        REGIME,
            "rationale":     RATIONALES.get(symbol),
        })
    client.table("trades").upsert(trade_rows).execute()
    print(f"  ✓ trades: {len(trade_rows)} orders")

    print("Done. Visit /api/performance to verify.")


if __name__ == "__main__":
    seed()
