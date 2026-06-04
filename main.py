"""
AI Investor V3 — Main Entry Point

Pipeline:
  1. Kill-switch check
  2. Fetch portfolio (Robinhood)
  3. Fetch market data (Polygon)
  4. Compute quant scores (deterministic)
  5. Run 7-agent pipeline (Claude)
  6. Execute trades (Robinhood)
  7. Log to CSV + decision journal
"""

from market_data import get_market_snapshot
from analysis   import get_trade_decisions
from quant_engine import score_all_tickers
from execute    import execute_trades, get_portfolio_summary, log_trades, get_trade_history
from journal    import check_kill_switches, record_trade


def run_daily_cycle():
    print("\n" + "=" * 60)
    print("🤖  AI INVESTOR V3 — DAILY CYCLE STARTING")
    print("=" * 60)

    # ── Step 1: Portfolio ─────────────────────────────────────────────────────
    print("\n📊  Step 1: Fetching portfolio...")
    portfolio = get_portfolio_summary()
    print(f"   Cash: ${portfolio['cash']:,.2f}")
    print(f"   Positions: {len(portfolio['positions'])}")
    print(f"   Total Value: ${portfolio['total_value']:,.2f}")

    # ── Step 2: Kill switches ─────────────────────────────────────────────────
    print("\n🛡️   Step 2: Checking kill switches...")
    kill_active, kill_reason = check_kill_switches(portfolio)
    if kill_active:
        print(f"   🛑 KILL SWITCH ACTIVE: {kill_reason}")
    else:
        print("   ✅ All clear.")

    # ── Step 3: Market data ───────────────────────────────────────────────────
    print("\n📈  Step 3: Fetching market data...")
    market_data = get_market_snapshot()
    print(
        f"   {len(market_data['prices'])} tickers | "
        f"{len(market_data['news'])} news articles | "
        f"{len(market_data.get('news_discovered', {}))} news-discovered"
    )

    if not market_data["prices"]:
        print("\n   No price data available (market may be closed). Skipping.")
        print("=" * 60 + "\n")
        return

    # ── Step 4: Quant scores ──────────────────────────────────────────────────
    print("\n🔢  Step 4: Computing quant scores...")
    quant_scores = score_all_tickers(market_data)
    top5 = sorted(quant_scores.items(), key=lambda x: x[1].get("composite_score", 0), reverse=True)[:5]
    print("   Top 5 by composite score:")
    for ticker, s in top5:
        print(
            f"   {ticker}: {s.get('composite_score')} "
            f"(mom={s.get('momentum_score')} q={s.get('quality_score')} "
            f"val={s.get('valuation_score')} vol={s.get('volatility')}%)"
        )

    # ── Step 5: 7-agent pipeline ──────────────────────────────────────────────
    print("\n🧠  Step 5: Running 7-agent pipeline...")
    trade_history = get_trade_history()
    decisions = get_trade_decisions(portfolio, market_data, quant_scores, trade_history)

    if not decisions:
        print("\n   No trades today.")
        print("=" * 60 + "\n")
        return

    print(f"\n   {len(decisions)} trade decision(s):")
    for d in decisions:
        print(
            f"   → {d['action']} {d['ticker']} "
            f"target={d.get('target_weight', 0):.1%} "
            f"source={d.get('source_of_capital', '?')} | {d.get('rationale', '')}"
        )

    # Write decisions for external consumers (e.g. scheduled routine executing via MCP)
    import json as _json
    with open("pending_decisions.json", "w") as _f:
        _json.dump(decisions, _f, indent=2)

    # ── Step 6: Execute ───────────────────────────────────────────────────────
    if kill_active:
        print("\n⛔  Step 6: Kill switch active — skipping execution.")
    else:
        print("\n⚡  Step 6: Executing trades...")
        execute_trades(decisions, portfolio, market_data["prices"])

    # ── Step 7: Log ───────────────────────────────────────────────────────────
    print("\n📝  Step 7: Logging decisions...")
    log_trades(decisions, portfolio, market_data["prices"])

    for d in decisions:
        if d.get("action") in ("BUY", "SELL"):
            trade_id = record_trade(
                ticker         = d["ticker"],
                action         = d["action"],
                target_weight  = d.get("target_weight", 0),
                thesis         = d.get("rationale", ""),
                anti_thesis    = d.get("anti_thesis", ""),
                catalysts      = d.get("catalysts", []),
                confidence     = d.get("confidence", 5),
                expected_return = d.get("expected_return", 0),
                invalidates_if = d.get("invalidates_if", []),
            )
            print(f"   📔 Journal entry: {trade_id} ({d['action']} {d['ticker']})")

    print("\n✅  Daily cycle complete.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    print("🚀  AI Investor V3 — running cycle...")
    run_daily_cycle()
