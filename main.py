"""
AI Investor V3 — Main Entry Point

Pipeline:
  1. Kill-switch check
  2. Fetch portfolio (Robinhood)
  3. Fetch market data — ABORT if today's snapshot isn't ready
  4. Compute quant scores (deterministic)
  5. Run 7-agent pipeline (Claude)
  6. Execute trades (Robinhood)
  7. Log to CSV + decision journal + agent log + transaction history
  8. Publish to Supabase
  9. Write system_health.json (triggers alert.yml if any failures)
"""

import json as _json
import uuid as _uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

from market_data  import get_market_snapshot
from analysis     import get_trade_decisions
from quant_engine import score_all_tickers
from execute      import execute_trades, get_portfolio_summary, log_trades, get_trade_history, _compute_qty, order_executed, StalePortfolioError, DRY_RUN
from journal      import check_kill_switches, record_trade, record_run, record_transaction, mark_pending_executed, mark_execution_started, get_recent_decisions, close_position, get_ticker_history, recently_exited, _load_list, TRANSACTIONS_FILE
from guardrails   import validate_decisions, enforce_sector_limits, enforce_min_holding_period, enforce_wash_sale_reentry, enforce_net_edge, flag_wash_sale_presale
from publish      import publish_to_supabase
from health       import HealthTracker, OK, DEGRADED, FAILED, ABORTED


def apply_pm_backstop(decisions: list, portfolio: dict, pipeline_state: dict) -> list[str]:
    """Force-sell deteriorating positions when 3 independent signals agree.

    Returns list of tickers that were auto-exited.
    Mutates *decisions* in-place by appending SELL entries.
    """
    position_reviews = pipeline_state.get("position_reviews", {})
    devil_map = pipeline_state.get("devils_advocate", {})
    auto_exits = []
    for holding in portfolio.get("positions", []):
        t = holding["symbol"]
        pr = position_reviews.get(t, {})
        da = devil_map.get(t, {})
        already_selling = any(
            d.get("ticker") == t and d.get("action") == "SELL"
            for d in decisions
        )
        if (not already_selling
                and pr.get("recommended_action") in ("REDUCE", "EXIT")
                and (pr.get("hold_score") or 10) < 5
                and da.get("recommend_reject", False)):
            auto_exits.append(t)
            decisions.append({
                "ticker": t,
                "action": "SELL",
                "target_weight": 0.0,
                "source_of_capital": "exit_deteriorating_position",
                "expected_return": 0.0,
                "rationale": (
                    f"Auto-exit: position_review={pr.get('recommended_action')} "
                    f"hold={pr.get('hold_score')}/10 alpha={pr.get('remaining_alpha')}, "
                    f"DA risk={da.get('overall_risk_score')}/10 reject=True — "
                    f"3-signal override of PM HOLD"
                ),
            })
    if auto_exits:
        print(f"   ⚡ PM override — auto-SELL {auto_exits} "
              f"(position_review + DA 3-signal agreement, PM chose HOLD)")
    return auto_exits


def run_daily_cycle():
    print("\n" + "=" * 60)
    print("🤖  AI INVESTOR V3 — DAILY CYCLE STARTING")
    print("=" * 60)

    run_id    = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_start = datetime.now(timezone.utc).isoformat()
    today     = datetime.now(_ET).strftime("%Y-%m-%d")  # ET matches preflight_gate and pending_decisions date

    health = HealthTracker(run_id, today)

    # ── Step 1: Portfolio ─────────────────────────────────────────────────────
    print("\n📊  Step 1: Fetching portfolio...")
    try:
        portfolio = get_portfolio_summary()
    except StalePortfolioError as e:
        print(f"\n   🚨 PIPELINE ABORTED: {e}")
        health.record("portfolio", FAILED, message=str(e)[:300])
        health.record("pipeline", ABORTED,
                      message="Aborted before sizing orders — mcp_portfolio.json is stale or undated.")
        health.save()
        print(f"\n   📋 system_health.json written (overall={health.overall_status})")
        print("=" * 60 + "\n")
        return
    print(f"   Cash: ${portfolio['cash']:,.2f}")
    print(f"   Positions: {len(portfolio['positions'])}")
    print(f"   Total Value: ${portfolio['total_value']:,.2f}")

    if portfolio.get("total_value", 0) > 0:
        health.record("portfolio", OK,
                      total_value=portfolio["total_value"],
                      cash=portfolio["cash"],
                      positions=len(portfolio["positions"]))
    else:
        health.record("portfolio", FAILED,
                      message="Portfolio fetch returned zero total value — Robinhood MCP may be unavailable")

    # ── Step 2: Kill switches ─────────────────────────────────────────────────
    print("\n🛡️   Step 2: Checking kill switches...")
    kill_active, kill_reason = check_kill_switches(portfolio)
    if kill_active:
        print(f"   🛑 KILL SWITCH ACTIVE: {kill_reason}")
        health.record("kill_switch", DEGRADED,
                      message=f"Kill switch active: {kill_reason}",
                      reason=kill_reason)
    else:
        print("   ✅ All clear.")
        health.record("kill_switch", OK)

    # ── Step 3: Market data ───────────────────────────────────────────────────
    print("\n📈  Step 3: Fetching market data...")
    market_data = get_market_snapshot()
    source    = market_data.get("_source", "unknown")
    data_date = market_data.get("_data_date", market_data.get("date", "unknown"))
    prices    = market_data.get("prices", {})
    history   = market_data.get("history", {})
    print(
        f"   {len(prices)} tickers | "
        f"{len(market_data.get('news', []))} news articles | "
        f"source={source} | data_date={data_date}"
    )

    # Measure history depth
    history_depths = [len(h) for h in history.values()] if history else []
    min_depth = min(history_depths) if history_depths else 0
    avg_depth = round(sum(history_depths) / len(history_depths), 1) if history_depths else 0

    # ── PRE-FLIGHT ABORT: market data must be from today with enough history ──
    # If data isn't ready, running agents produces all-50 quant scores, empty
    # research, and a guaranteed no-trade outcome.  Better to abort and alert.
    abort_reasons = []
    if data_date != today:
        abort_reasons.append(f"data is from {data_date}, not today ({today})")
    if min_depth < 22:
        abort_reasons.append(f"history depth is {min_depth} bars — need 22+ for any quant calculation")

    if abort_reasons:
        msg = " | ".join(abort_reasons)
        print(f"\n   🚨 PIPELINE ABORTED: {msg}")
        print("   Waiting for GitHub Actions market data job (market_data.yml) to complete.")
        print("   The routine should not re-run until market_snapshot.json is updated for today.")

        health.record("market_data", FAILED,
                      message=msg,
                      source=source,
                      data_date=data_date,
                      history_min_bars=min_depth)
        health.record("pipeline", ABORTED,
                      message=f"Aborted before agents ran — market data not ready. {msg}")
        health.save()
        print(f"\n   📋 system_health.json written (overall={health.overall_status})")
        print("=" * 60 + "\n")
        return

    # Data is fresh — record quality level
    if min_depth >= 126:
        health.record("market_data", OK,
                      source=source, data_date=data_date,
                      tickers=len(prices),
                      history_min_bars=min_depth, history_avg_bars=avg_depth)
    elif min_depth >= 63:
        health.record("market_data", DEGRADED,
                      message=f"History depth {min_depth} bars — 6M momentum unavailable (need 127+)",
                      source=source, data_date=data_date,
                      history_min_bars=min_depth)
    else:
        health.record("market_data", DEGRADED,
                      message=f"History depth {min_depth} bars — only 1M momentum available (need 64+ for 3M)",
                      source=source, data_date=data_date,
                      history_min_bars=min_depth)

    if not prices:
        print("\n   No price data available (market may be closed). Skipping.")
        health.record("pipeline", ABORTED, message="No price data — market may be closed")
        health.save()
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

    all_scores  = [s.get("composite_score", 50) for s in quant_scores.values()]
    real_count  = sum(1 for s in all_scores if s != 50.0)
    all_neutral = real_count == 0

    if all_neutral:
        health.record("quant_scores", FAILED,
                      message="All quant scores are 50.0 — no historical data reached the engine",
                      real_scores=0, total=len(all_scores))
        print("   🚨 WARNING: All quant scores are 50.0 — no historical data")
    elif real_count < len(all_scores) * 0.5:
        health.record("quant_scores", DEGRADED,
                      message=f"Only {real_count}/{len(all_scores)} tickers have real quant scores",
                      real_scores=real_count, total=len(all_scores))
    else:
        health.record("quant_scores", OK, real_scores=real_count, total=len(all_scores))

    # ── Step 5: 7-agent pipeline ──────────────────────────────────────────────
    print("\n🧠  Step 5: Running 7-agent pipeline...")
    trade_history = get_trade_history()

    recent_entries = get_recent_decisions(n=200)
    prior_journal: dict = {}
    for entry in recent_entries:
        t = entry.get("ticker")
        if t and t not in prior_journal and entry.get("status") == "open":
            prior_journal[t] = entry

    # Phase 2 — outcome memory fed back to the agents. ticker_history gives the
    # Research Analyst each name's prior theses + realized outcomes; recently
    # exited names become a re-entry warning for the Portfolio Manager.
    seen_tickers   = {e.get("ticker") for e in recent_entries if e.get("ticker")}
    ticker_history = {t: get_ticker_history(t) for t in seen_tickers}
    exited_map     = recently_exited()

    decisions, pipeline_state = get_trade_decisions(
        portfolio, market_data, quant_scores, trade_history, prior_journal,
        ticker_history=ticker_history, recently_exited=exited_map,
    )

    # ── PM backstop: auto-exit when 3 signals agree ──────────────────────────
    apply_pm_backstop(decisions, portfolio, pipeline_state)

    # Pre-compute fractional qty
    for _d in decisions:
        _action = _d.get("action", "").upper()
        if _action in ("BUY", "SELL") and "target_weight" in _d:
            _d["qty"] = _compute_qty(
                _d["target_weight"], _action, _d["ticker"], portfolio, market_data["prices"]
            )

    # ── Validation gate: deterministic guardrails on LLM output ──────────────
    # Runs AFTER qty pre-computation (notional checks need qty) and BEFORE the
    # decisions reach pending_decisions.json / execution. See guardrails.py.
    # Load the executed-trade log ONCE and pass it to every guard that needs it
    # (validate's GFV check + both turnover guards) — one disk read and a single
    # consistent view, instead of three independent reads of the same file.
    _txs = _load_list(TRANSACTIONS_FILE)

    decisions, validation_report = validate_decisions(
        decisions, portfolio, market_data["prices"],
        pipeline_state.get("candidates", []), kill_active, transactions=_txs,
    )

    # Turnover / tax discipline (CA top-bracket taxable account). Every sale is a
    # short-term gain (~54%), so cut round-trip churn: block SELLs of names bought
    # < 5 trading days ago, and BUYs of names sold < 30 calendar days ago
    # (wash-sale + anti-churn). Risk exits are exempt via kill_active. These run
    # BEFORE the sector cap so the cap projects against the SELL set that will
    # actually execute — otherwise a SELL dropped here after the cap freed its
    # sector budget could let a same-sector BUY breach the 25% limit.
    decisions, holding_rejected = enforce_min_holding_period(decisions, portfolio, transactions=_txs, kill_active=kill_active)
    decisions, reentry_rejected = enforce_wash_sale_reentry(decisions, transactions=_txs)

    # Sector cap (25%) — a code-level control (the limit otherwise lives only in
    # the PM prompt). Runs after turnover filtering so it sees the post-turnover set.
    decisions, sector_rejected = enforce_sector_limits(decisions, portfolio)

    # Net-edge gate (#6) — reject a BUY whose expected return, after round-trip
    # cost + CA short-term tax, falls below the floor. No-op until the PM emits an
    # expected_return; SELLs exempt. Runs last (final economic filter).
    decisions, netedge_rejected = enforce_net_edge(decisions, market_data["prices"])

    # Wash-sale PRE-sale FLAG (A6) — the post-sale re-buy block above covers one
    # side of IRS §1091; this flags (never blocks) a loss exit within 30d of a
    # purchase, the other side. Flag-and-allow: a wash sale only defers the loss,
    # so a risk/conviction exit must not be blocked to preserve tax timing. The
    # annotation rides on the SELL decision into pending_decisions.json for audit.
    # OBSERVATIONAL/annotation only — must never break the trade path. A failure
    # here leaves decisions exactly as the blocking guardrails produced them.
    try:
        decisions, washsale_flagged = flag_wash_sale_presale(
            decisions, market_data["prices"], transactions=_txs)
        if washsale_flagged:
            health.record("wash_sale_presale", DEGRADED,
                          message=f"{len(washsale_flagged)} loss SELL(s) within 30d of "
                                  "purchase flagged (allowed; loss deferred per §1091)",
                          flagged=washsale_flagged)
    except Exception as _e:
        print(f"   ⚠ wash-sale pre-sale flag skipped: {_e}")

    for r in holding_rejected + reentry_rejected + sector_rejected + netedge_rejected:
        validation_report["rejected"].append(
            {"ticker": r.get("ticker", "?"), "action": r.get("action", "?"),
             "reason": r.get("rejected_reason", "turnover/sector/net-edge guard")})

    _interventions = (validation_report["rejected"] + validation_report["modified"]
                      + validation_report["skipped"])
    if _interventions:
        health.record("decision_validation", DEGRADED,
                      message=f"{len(validation_report['rejected'])} rejected, "
                              f"{len(validation_report['modified'])} clamped, "
                              f"{len(validation_report['skipped'])} skipped by guardrails",
                      **validation_report)
    else:
        health.record("decision_validation", OK, passed=validation_report["passed"])

    # ── Agent health checks ───────────────────────────────────────────────────

    # Agent 1: Market Regime
    regime = pipeline_state.get("regime", {})
    if not regime:
        health.record("agent_1_regime", FAILED, message="No regime output returned")
    elif regime.get("confidence", 0) < 25:
        health.record("agent_1_regime", DEGRADED,
                      message=f"Low confidence: {regime.get('confidence')}/100 — insufficient market data",
                      regime=regime.get("regime"), confidence=regime.get("confidence"))
    else:
        health.record("agent_1_regime", OK,
                      regime=regime.get("regime"), confidence=regime.get("confidence"))

    # Agent 2: Research
    research    = pipeline_state.get("research", {})
    empty_rsrch = sum(1 for v in research.values() if not v.get("thesis", "").strip())
    if not research:
        health.record("agent_2_research", FAILED, message="No research output")
    elif empty_rsrch == len(research):
        health.record("agent_2_research", FAILED,
                      message=f"All {empty_rsrch}/{len(research)} research responses returned empty thesis",
                      empty=empty_rsrch, total=len(research))
    elif empty_rsrch > 0:
        health.record("agent_2_research", DEGRADED,
                      message=f"{empty_rsrch}/{len(research)} research responses had empty thesis",
                      empty=empty_rsrch, total=len(research))
    else:
        health.record("agent_2_research", OK, total=len(research))

    # Agent 3: Earnings
    earnings      = pipeline_state.get("earnings", {})
    default_earn  = sum(1 for v in earnings.values()
                        if v.get("earnings_alpha_score") == 5 and not v.get("key_catalysts_90d"))
    if not earnings:
        health.record("agent_3_earnings", FAILED, message="No earnings output")
    elif default_earn == len(earnings):
        health.record("agent_3_earnings", FAILED,
                      message=f"All {default_earn}/{len(earnings)} earnings responses returned defaults",
                      default=default_earn, total=len(earnings))
    elif default_earn > len(earnings) * 0.8:
        health.record("agent_3_earnings", DEGRADED,
                      message=f"{default_earn}/{len(earnings)} earnings responses are defaults",
                      default=default_earn, total=len(earnings))
    else:
        health.record("agent_3_earnings", OK, total=len(earnings))

    # Agent 4: Devil's Advocate
    da       = pipeline_state.get("devils_advocate", {})
    empty_da = sum(1 for v in da.values() if not v.get("bear_case", "").strip())
    if not da:
        health.record("agent_4_devils_advocate", FAILED, message="No devil's advocate output")
    elif empty_da == len(da):
        health.record("agent_4_devils_advocate", FAILED,
                      message=f"All {empty_da}/{len(da)} devil's advocate responses empty",
                      empty=empty_da, total=len(da))
    elif empty_da > 0:
        health.record("agent_4_devils_advocate", DEGRADED,
                      message=f"{empty_da}/{len(da)} devil's advocate responses empty",
                      empty=empty_da, total=len(da))
    else:
        health.record("agent_4_devils_advocate", OK, total=len(da))

    # Agent 5: Position Review
    position_reviews  = pipeline_state.get("position_reviews", {})
    existing_pos      = portfolio.get("positions", [])
    if existing_pos and not position_reviews:
        health.record("agent_5_position_review", FAILED,
                      message=f"No reviews despite {len(existing_pos)} open positions",
                      positions=len(existing_pos))
    else:
        reduces = sum(1 for v in position_reviews.values()
                      if v.get("recommended_action") in ("REDUCE", "EXIT"))
        health.record("agent_5_position_review", OK,
                      reviewed=len(position_reviews), reduce_exit_recommended=reduces)

    # Agent 6: Portfolio Manager
    pm_proposed = pipeline_state.get("portfolio_manager_proposed", [])
    reduces     = sum(1 for v in position_reviews.values()
                      if v.get("recommended_action") in ("REDUCE", "EXIT"))
    if reduces > 0 and len(pm_proposed) == 0 and not decisions:
        health.record("agent_6_portfolio_manager", DEGRADED,
                      message=f"PM proposed 0 trades despite {reduces} REDUCE/EXIT from position review — likely data starvation",
                      position_review_reduce_exit=reduces, proposed=0)
    else:
        health.record("agent_6_portfolio_manager", OK,
                      proposed=len(pm_proposed), final_decisions=len(decisions))

    # Agent 7: CRO
    cro = pipeline_state.get("cro", {})
    if not cro:
        health.record("agent_7_cro", FAILED, message="No CRO output")
    elif cro.get("api_failed"):
        health.record("agent_7_cro", DEGRADED, message="CRO API error — trades BLOCKED (safety default)",
                      approved=cro.get("approved"),
                      vetoed=cro.get("rejected_tickers", []))
    else:
        health.record("agent_7_cro", OK,
                      approved=cro.get("approved"),
                      vetoed=cro.get("rejected_tickers", []),
                      risk_budget=cro.get("risk_budget_used"))

    # ── Agent log (written every run, even no-trade days) ────────────────────
    pipeline_state["run_id"]             = run_id
    pipeline_state["timestamp"]          = run_start
    pipeline_state["kill_switch_active"] = kill_active
    pipeline_state["market_data_source"] = source
    pipeline_state["market_data_date"]   = data_date
    pipeline_state["portfolio_snapshot"] = {
        "cash":        portfolio["cash"],
        "total_value": portfolio["total_value"],
        "positions":   portfolio["positions"],
    }
    # T2.1: net-edge gate rejection count — logged per run so deliberation_stats
    # can report how often the gate drops a BUY (validates it isn't silently
    # starving the book; a live gate with zero measured effect is its own risk).
    pipeline_state["net_edge_rejected"] = len(netedge_rejected)
    record_run(run_id, pipeline_state)
    print(f"   📋 Agent log written (run_id={run_id})")

    # Forecast ledger (#2) — log every agent's structured forecasts for later
    # calibration. OBSERVATIONAL: logging only, never affects a decision, and
    # never raises into the pipeline.
    try:
        from calibration import log_forecasts
        _n_fc = log_forecasts(run_id, today, pipeline_state,
                              pipeline_state.get("candidates", []), market_data["prices"])
        if _n_fc:
            print(f"   🧮 Logged {_n_fc} forecast(s) to forecasts.jsonl")
    except Exception as _e:
        print(f"   ⚠ forecast logging skipped: {_e}")

    # Reproducibility manifest (#A12) — resolved model snapshots + token usage +
    # sampling params + verbatim prompts for this run. Observational; never raises.
    try:
        from analysis import export_reproducibility
        export_reproducibility(run_id=run_id, date=today)
        print("   🔁 Reproducibility manifest written (reproducibility.json)")
    except Exception as _e:
        print(f"   ⚠ reproducibility manifest skipped: {_e}")

    with open("pending_decisions.json", "w") as _f:
        _json.dump({
            "run_id":               run_id,
            "date":                 today,
            "generated_at":         run_start,
            "execution_started_at": None,
            "executed_at":          None,
            "decisions":            decisions,
        }, _f, indent=2)

    if not decisions:
        print("\n   No trades today.")

        # ── Step 8: Publish ───────────────────────────────────────────────────
        print("\n🌐  Step 8: Publishing to Supabase...")
        try:
            publish_to_supabase(portfolio, quant_scores=quant_scores)
            health.record("supabase_publish", OK)
        except Exception as e:
            health.record("supabase_publish", FAILED,
                          message=str(e)[:200])
            print(f"   ⚠ Supabase publish skipped: {e}")

        _write_health(health)
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
    # Circuit breaker: total SELL-side notional > 50% of portfolio is anomalous.
    # The PM should never churn more than half the book in a single day.
    _sell_notional = sum(
        d.get("qty", 0) * market_data["prices"].get(d["ticker"], {}).get("close", 0)
        for d in decisions if d.get("action", "").upper() == "SELL"
    )
    _turnover_pct = _sell_notional / portfolio["total_value"] if portfolio["total_value"] else 0
    if _turnover_pct > 0.50:
        print(f"\n🚨 CIRCUIT BREAKER: proposed SELL notional ${_sell_notional:,.2f} = {_turnover_pct:.0%} of portfolio — exceeds 50% daily turnover limit. Halting execution.")
        health.record("execution", FAILED,
                      message=f"Circuit breaker: SELL notional {_turnover_pct:.0%} exceeds 50% daily limit",
                      sell_notional=round(_sell_notional, 2), turnover_pct=round(_turnover_pct, 4))
        _write_health(health)
        print("=" * 60 + "\n")
        return

    order_results: dict    = {}
    attempted: list        = []
    execution_errors: list = []

    if kill_active:
        attempted = [d for d in decisions if d.get("action", "").upper() == "SELL"]
        blocked   = [d for d in decisions if d.get("action", "").upper() == "BUY"]
        if blocked:
            print(f"\n⛔  Step 6: Kill switch active — blocking {len(blocked)} BUY(s).")
        if attempted:
            print(f"\n⚡  Step 6: Executing {len(attempted)} SELL(s) (kill switch active)...")
        else:
            print("\n⛔  Step 6: Kill switch active — no SELL orders to execute.")
    else:
        attempted = decisions
        print("\n⚡  Step 6: Executing trades...")

    if attempted:
        # Claim the run BEFORE the first order (stamp-first). In the cloud the
        # routine does this itself (and pushes the claim for cross-attempt
        # durability) before its MCP orders; locally main.py places the orders,
        # so it sets the claim here. DRY_RUN never claims — no orders happen.
        if not DRY_RUN:
            mark_execution_started(run_id)
        try:
            order_results = execute_trades(attempted, portfolio, market_data["prices"])
        except Exception as e:
            execution_errors.append(str(e))
            print(f"   ❌ Execution error: {e}")

    # An order only counts as executed when the broker returned an order id
    # (or this is a dry run). Rejections — insufficient buying power, halted
    # ticker, hard-block — come back without an id and must not be logged as
    # fills or reported as healthy.
    failed_orders = {t: r for t, r in order_results.items() if not order_executed(r)}
    executed_decisions = [
        d for d in attempted
        if d.get("action", "").upper() in ("BUY", "SELL")
        and order_executed(order_results.get(d.get("ticker")))
    ]

    # Stamp the idempotency lock as soon as anything was placed — a retry must
    # never double-fill those orders. If nothing was placed at all (every order
    # rejected, or execution crashed before the first order), leave it
    # unstamped so the next scheduled attempt can retry the day.
    any_placed = bool(executed_decisions)
    nothing_to_place = not order_results and not execution_errors
    if not DRY_RUN and (any_placed or nothing_to_place):
        mark_pending_executed(run_id)

    if execution_errors or failed_orders:
        fail_details = {}
        for t, r in failed_orders.items():
            if isinstance(r, dict) and r.get("detail"):
                fail_details[t] = str(r["detail"])[:200]
            elif isinstance(r, dict) and r.get("blocked"):
                fail_details[t] = "hard-blocked ticker"
            else:
                fail_details[t] = f"no broker order id (response: {str(r)[:120]})"
        for t, msg in fail_details.items():
            print(f"   ❌ Order NOT executed: {t} — {msg}")

        status = DEGRADED if executed_decisions else FAILED
        message = f"{len(failed_orders)} of {len(order_results)} order(s) failed"
        if execution_errors:
            message += f"; execution errors: {'; '.join(execution_errors)}"
        health.record("execution", status,
                      message=message,
                      failed_orders=fail_details,
                      errors=execution_errors,
                      executed=len(executed_decisions),
                      decisions=len(decisions), dry_run=DRY_RUN)
    else:
        health.record("execution", OK,
                      decisions=len(decisions),
                      executed=len(executed_decisions),
                      dry_run=DRY_RUN)

    # ── Step 7: Log ───────────────────────────────────────────────────────────
    print("\n📝  Step 7: Logging decisions...")
    log_trades(executed_decisions, portfolio, market_data["prices"],
               broker_order_ids=order_results, run_id=run_id)

    regime_str = pipeline_state.get("regime", {}).get("regime", "")

    for d in executed_decisions:
        if d.get("action") not in ("BUY", "SELL"):
            continue

        ticker        = d["ticker"]
        action        = d["action"]
        target_weight = d.get("target_weight", 0)
        price         = market_data["prices"].get(ticker, {}).get("close", 0)
        qty           = d.get("qty") or _compute_qty(target_weight, action, ticker, portfolio, market_data["prices"])
        total_value   = round(qty * price, 2) if qty and price else None
        broker_order  = order_results.get(ticker, {})
        broker_id     = broker_order.get("id") if isinstance(broker_order, dict) else None

        record_transaction({
            "transaction_id":         str(_uuid.uuid4()),
            "run_id":                 run_id,
            "date":                   today,
            "timestamp":              datetime.now(timezone.utc).isoformat(),
            "ticker":                 ticker,
            "action":                 action,
            "qty":                    qty,
            "price":                  round(price, 4) if price else None,
            "total_value":            total_value,
            "target_weight":          target_weight,
            "portfolio_value_before": portfolio["total_value"],
            "source_of_capital":      d.get("source_of_capital", ""),
            "regime":                 regime_str,
            "regime_confidence":      pipeline_state.get("regime", {}).get("confidence"),
            "rationale":              d.get("rationale", ""),
            "research_confidence":    pipeline_state.get("research", {}).get(ticker, {}).get("confidence"),
            "earnings_alpha_score":   pipeline_state.get("earnings", {}).get(ticker, {}).get("earnings_alpha_score"),
            "cro_risk_budget":        pipeline_state.get("cro", {}).get("risk_budget_used"),
            "broker_order_id":        broker_id,
            "dry_run":                DRY_RUN,
        })

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
            run_id          = run_id,
        )
        print(f"   📔 Journal entry: {trade_id} ({action} {ticker})")

        # ── Close the feedback loop: a SELL realizes the outcome of the matching
        # open BUY thesis (actual_return / thesis_correct). avg_price (cost basis)
        # comes from the broker position; exit_price from today's close.
        if action == "SELL":
            avg_price = next(
                (float(p.get("avg_price", 0) or 0) for p in portfolio["positions"]
                 if p.get("symbol") == ticker), 0.0)
            closed_id = close_position(
                ticker     = ticker,
                exit_price = price,
                avg_price  = avg_price,
                full_exit  = (float(target_weight or 0) == 0),
                run_id     = run_id,
            )
            if closed_id:
                print(f"   📕 Closed thesis {closed_id} for {ticker} "
                      f"(realized vs ${avg_price:.2f} cost basis)")

    # ── Step 8: Publish to Supabase ───────────────────────────────────────────
    print("\n🌐  Step 8: Publishing to Supabase...")
    try:
        publish_to_supabase(portfolio, quant_scores=quant_scores)
        health.record("supabase_publish", OK)
    except Exception as e:
        health.record("supabase_publish", FAILED, message=str(e)[:200])
        print(f"   ⚠ Supabase publish skipped: {e}")

    # ── Step 9: Write system_health.json ─────────────────────────────────────
    _write_health(health)

    print("\n✅  Daily cycle complete.")
    print("=" * 60 + "\n")


def _write_health(health: HealthTracker):
    data = health.save()
    status = data["overall_status"]
    icon   = {"OK": "✅", "DEGRADED": "⚠️", "FAILED": "❌", "ABORTED": "🚫"}.get(status, "❓")
    print(f"\n{icon}  system_health.json written — overall_status={status}")
    if data["alerts"]:
        for alert in data["alerts"]:
            print(f"   {alert}")


if __name__ == "__main__":
    print("🚀  AI Investor V3 — running cycle...")
    run_daily_cycle()
