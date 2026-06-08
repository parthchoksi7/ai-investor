"""
publish.py — Push daily portfolio snapshot to Supabase.

Called as Step 8 in main.py after trades are logged. Reads local JSON files,
computes metrics, and upserts to three Supabase tables:
  - portfolio_snapshots  (one row per day, keyed by date)
  - trades               (append-only via upsert, keyed by transaction_id)
  - positions            (current holdings, replaced wholesale each run)

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in environment.
Silently skips if either is missing (local dev without Supabase configured).
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

STARTING_CAPITAL = 500.0
TRANSACTIONS_FILE = "transactions.json"
PEAK_FILE = "portfolio_peak.json"
AGENT_LOG_FILE = "agent_log.json"


def _load(path: str, default):
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return default


def _fetch_spy_close(polygon_key: str) -> float | None:
    """Fetch SPY's previous-day closing price from Polygon. Returns None on any failure."""
    url = f"https://api.polygon.io/v2/aggs/ticker/SPY/prev?adjusted=true&apiKey={polygon_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-investor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if results:
            return float(results[0].get("c", 0))
    except Exception:
        pass
    return None


def _get_spy_cumulative(supabase_client, spy_close: float | None) -> float | None:
    """Compute SPY cumulative return vs inception (first row with a non-null spy_close)."""
    if spy_close is None:
        return None
    try:
        resp = (
            supabase_client.table("portfolio_snapshots")
            .select("spy_close")
            .not_.is_("spy_close", "null")
            .order("date", desc=False)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return 0.0  # this is the first snapshot with SPY data — baseline = 0%
        inception_spy = float(rows[0]["spy_close"])
        if inception_spy <= 0:
            return None
        return round((spy_close - inception_spy) / inception_spy * 100, 4)
    except Exception:
        return None


def publish_to_supabase(portfolio: dict | None = None) -> None:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        print("   Supabase not configured — skipping publish.")
        return

    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
    except ImportError:
        print("   supabase package not installed — skipping publish. Run: pip install supabase")
        return

    # ── Portfolio state ────────────────────────────────────────────────────────
    if portfolio is None:
        portfolio = _load("mcp_portfolio.json", {"cash": STARTING_CAPITAL, "total_value": STARTING_CAPITAL, "positions": []})

    total_value = float(portfolio.get("total_value", STARTING_CAPITAL))
    cash        = float(portfolio.get("cash", STARTING_CAPITAL))
    positions   = portfolio.get("positions", [])

    # ── Drawdown ───────────────────────────────────────────────────────────────
    peak_data = _load(PEAK_FILE, {})
    peak      = float(peak_data.get("peak", total_value))
    drawdown  = max(0.0, (peak - total_value) / peak * 100) if peak > 0 else 0.0

    # ── Regime (from last agent log entry) ────────────────────────────────────
    regime = ""
    logs = _load(AGENT_LOG_FILE, [])
    if logs:
        last_log = logs[-1]
        regime_data = last_log.get("regime", {})
        if isinstance(regime_data, dict):
            regime = regime_data.get("regime", "")

    # ── SPY benchmark ──────────────────────────────────────────────────────────
    polygon_key = os.getenv("POLYGON_API_KEY")
    spy_close   = _fetch_spy_close(polygon_key) if polygon_key else None
    spy_cumulative = _get_spy_cumulative(client, spy_close)

    # ── Upsert portfolio snapshot ──────────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    cumulative_return = round((total_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 4)

    snapshot: dict = {
        "date":                      today,
        "total_value":               round(total_value, 2),
        "cash":                      round(cash, 2),
        "num_positions":             len(positions),
        "cumulative_return_pct":     cumulative_return,
        "drawdown_pct":              round(drawdown, 4),
        "regime":                    regime or None,
    }
    if spy_close is not None:
        snapshot["spy_close"] = round(spy_close, 4)
    if spy_cumulative is not None:
        snapshot["spy_cumulative_return_pct"] = spy_cumulative

    client.table("portfolio_snapshots").upsert(snapshot).execute()
    print(f"   📊 Snapshot published: value=${total_value:,.2f} return={cumulative_return:+.2f}%"
          + (f" spy={spy_cumulative:+.2f}%" if spy_cumulative is not None else ""))

    # ── Upsert positions (replace wholesale) ──────────────────────────────────
    client.table("positions").delete().neq("ticker", "___never___").execute()
    if positions:
        pos_rows = []
        for p in positions:
            ticker        = p.get("symbol", "")
            qty           = float(p.get("qty", 0))
            avg_cost      = float(p.get("avg_price", 0))
            current_price = float(p.get("current_price", 0))
            market_value  = float(p.get("market_value", 0))

            unrealized_pct = 0.0
            if avg_cost > 0:
                unrealized_pct = round((current_price - avg_cost) / avg_cost * 100, 4)

            weight_pct = round(market_value / total_value * 100, 4) if total_value > 0 else 0.0

            pos_rows.append({
                "ticker":         ticker,
                "weight_pct":     weight_pct,
                "quantity":       qty,
                "avg_cost":       avg_cost,
                "current_price":  current_price,
                "unrealized_pct": unrealized_pct,
                "updated_at":     datetime.now().isoformat(),
            })
        client.table("positions").insert(pos_rows).execute()

    # ── Upsert trades from transactions.json ──────────────────────────────────
    transactions = _load(TRANSACTIONS_FILE, [])
    if transactions:
        trade_rows = [
            {
                "id":                  tx.get("transaction_id"),
                "date":                tx.get("date"),
                "ticker":              tx.get("ticker"),
                "action":              tx.get("action"),
                "qty":                 tx.get("qty"),
                "price":               tx.get("price"),
                "total_value":         tx.get("total_value"),
                "target_weight":       tx.get("target_weight"),
                "regime":              tx.get("regime"),
                "rationale":           tx.get("rationale"),
                "research_confidence": tx.get("research_confidence"),
            }
            for tx in transactions
            if tx.get("transaction_id")
        ]
        if trade_rows:
            client.table("trades").upsert(trade_rows).execute()
            print(f"   📋 {len(trade_rows)} trade(s) synced.")

    print("   ✅ Supabase publish complete.")
