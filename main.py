"""
AI Investor V3 — Main Entry Point

Pipeline:
  1. Kill-switch check
  2. Fetch portfolio (Robinhood)
  3. Fetch market data (Polygon)
  4. Compute quant scores (deterministic)
  5. Run 7-agent pipeline (Claude)
  6. Execute trades (Robinhood)
  7. Log to CSV + decision journal + agent log + transaction history
"""

import json as _json
import uuid as _uuid
from datetime import datetime, timezone

from market_data  import get_market_snapshot
from analysis     import get_trade_decisions
from quant_engine import score_all_tickers
from execute      import execute_trades, get_portfolio_summary, log_trades, get_trade_history, _compute_qty, DRY_RUN
from journal      import check_kill_switches, record_trade, record_run, record_transaction, mark_pending_executed
from publish      import publish_to_supabase


def run_daily_cycle():
    print("\n" + "=" * 60)
    print("🤖  AI INVESTOR V3 — DAILY CYCLE STARTING")
    print("=" * 60)

    run_id    = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_start = datetime.now(timezone.utc).isoformat()
    today     = datetime.now().strftime("%Y-%m-%d")

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
    decisions, pipeline_state = get_trade_decisions(portfolio, market_data, quant_scores, trade_history)

    # ── Agent log (written every run, even no-trade days) ────────────────────
    pipeline_state["run_id"]            = run_id
    pipeline_state["timestamp"]         = run_start
    pipeline_state["kill_switch_active"] = kill_active
    pipeline_state["portfolio_snapshot"] = {
        "cash":        portfolio["cash"],
        "total_value": portfolio["total_value"],
        "positions":   portfolio["positions"],
    }
    record_run(run_id, pipeline_state)
    print(f"   📋 Agent log written (run_id={run_id})")

    # Always write pending_decisions.json (routine reads this).
    # Wrap in a metadata envelope so the routine can verify freshness and
    # stamp executed_at after completing MCP orders to prevent double-execution.
    with open("pending_decisions.json", "w") as _f:
        _json.dump({
            "run_id":      run_id,
            "date":        today,
            "generated_at": run_start,
            "executed_at": None,
            "decisions":   decisions,
        }, _f, indent=2)

    if not decisions:
        print("\n   No trades today.")
        # ── Step 8: Publish snapshot even on no-trade days ────────────────────
        print("\n🌐  Step 8: Publishing to Supabase...")
        publish_to_supabase(portfolio)
        print("\n✅  Daily cycle complete.")
        print("=" * 60 + "\n")
        return

    print(f"\n   {len(decisions)} trade decision(s):")
    for d in decisions:
        print(
            f"   → {d['action']} {d['ticker']} "
            f"target={d.get('target_weight', 0):.1%} "
            f"source={d.get('source_of_capital', '?')} | {d.get('rationale', '')}"
        )

    # ── Step 6: Execute ───────────────────────────────────────────────────────
    order_results: dict = {}
    executed_decisions: list = []

    if kill_active:
        # SELLs always execute — kill switch only blocks new BUYs.
        sell_only = [d for d in decisions if d.get("action", "").upper() == "SELL"]
        blocked   = [d for d in decisions if d.get("action", "").upper() == "BUY"]
        if blocked:
            print(f"\n⛔  Step 6: Kill switch active — blocking {len(blocked)} BUY(s).")
        if sell_only:
            print(f"\n⚡  Step 6: Executing {len(sell_only)} SELL(s) (kill switch active)...")
            order_results      = execute_trades(sell_only, portfolio, market_data["prices"])
            executed_decisions = sell_only
            if not DRY_RUN:
                mark_pending_executed(run_id)
        else:
            print("\n⛔  Step 6: Kill switch active — no SELL orders to execute.")
    else:
        print("\n⚡  Step 6: Executing trades...")
        order_results      = execute_trades(decisions, portfolio, market_data["prices"])
        executed_decisions = decisions
        if not DRY_RUN:
            mark_pending_executed(run_id)

    # ── Step 7: Log ───────────────────────────────────────────────────────────
    # Log only what was actually sent to the broker — blocked BUYs are omitted.
    print("\n📝  Step 7: Logging decisions...")
    log_trades(executed_decisions, portfolio, market_data["prices"], broker_order_ids=order_results)

    regime = pipeline_state.get("regime", {}).get("regime", "")

    for d in executed_decisions:
        if d.get("action") not in ("BUY", "SELL"):
            continue

        ticker        = d["ticker"]
        action        = d["action"]
        target_weight = d.get("target_weight", 0)
        price         = market_data["prices"].get(ticker, {}).get("close", 0)
        qty           = _compute_qty(target_weight, action, ticker, portfolio, market_data["prices"])
        total_value   = round(qty * price, 2) if qty and price else None

        broker_order  = order_results.get(ticker, {})
        broker_id     = broker_order.get("id") if isinstance(broker_order, dict) else None

        # Detailed transaction history (for public performance display)
        record_transaction({
            "transaction_id":        str(_uuid.uuid4()),
            "run_id":                run_id,
            "date":                  today,
            "timestamp":             datetime.now(timezone.utc).isoformat(),
            "ticker":                ticker,
            "action":                action,
            "qty":                   qty,
            "price":                 round(price, 4) if price else None,
            "total_value":           total_value,
            "target_weight":         target_weight,
            "portfolio_value_before": portfolio["total_value"],
            "source_of_capital":     d.get("source_of_capital", ""),
            "regime":                regime,
            "regime_confidence":     pipeline_state.get("regime", {}).get("confidence"),
            "rationale":             d.get("rationale", ""),
            "research_confidence":   pipeline_state.get("research", {}).get(ticker, {}).get("confidence"),
            "earnings_alpha_score":  pipeline_state.get("earnings", {}).get(ticker, {}).get("earnings_alpha_score"),
            "cro_risk_budget":       pipeline_state.get("cro", {}).get("risk_budget_used"),
            "broker_order_id":       broker_id,
            "dry_run":               DRY_RUN,
        })

        # Decision journal (thesis tracking)
        trade_id = record_trade(
            ticker          = ticker,
            action          = action,
            target_weight   = target_weight,
            thesis          = pipeline_state.get("research", {}).get(ticker, {}).get("thesis", d.get("rationale", "")),
            anti_thesis     = pipeline_state.get("devils_advocate", {}).get(ticker, {}).get("bear_case", ""),
            catalysts       = pipeline_state.get("research", {}).get(ticker, {}).get("catalysts", []),
            confidence      = pipeline_state.get("research", {}).get(ticker, {}).get("confidence", 5),
            expected_return = d.get("expected_return", 0),
            invalidates_if  = pipeline_state.get("research", {}).get(ticker, {}).get("invalidates_if", []),
        )
        print(f"   📔 Journal entry: {trade_id} ({action} {ticker})")

    # ── Step 8: Publish to Supabase ───────────────────────────────────────────
    print("\n🌐  Step 8: Publishing to Supabase...")
    publish_to_supabase(portfolio)

    print("\n✅  Daily cycle complete.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    print("🚀  AI Investor V3 — running cycle...")
    run_daily_cycle()
