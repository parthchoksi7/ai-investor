"""
risk_watch.py — the daily SELL-only safety net (Phase 5 Stage B, §6.4/§6.7).

Runs on every trading day that is NOT the weekly rebalance (preflight_gate exit 30).
It is a DECISION GENERATOR, not a second order path (§6.4 — non-negotiable): its
decisions are written into the SAME ``pending_decisions.json`` idempotency envelope
and executed through the SAME journal claim → MCP orders → ``mark_pending_executed``
→ ``mark_transactions_live`` machinery the rebalance uses. A parallel "lightweight"
order path would fork the audit trail and shed every control the daily cycle took a
dozen incidents (Jun 9–17) to harden.

The trigger set is TIGHT, MECHANICAL, and LLM-FREE (§6.7 — the definition IS the
control; a loose "risk" definition reintroduces the churn the weekly cadence exists
to kill):

  FIRES →  per-position hard stop: close ≤ −``single_name_stop_pct`` (25%) from the
           broker cost basis (avg_price), evaluated on the LIVE Robinhood MCP price
           (P1-7: price triggers always fire; no snapshot dependency). Full exit,
           exempt from the min-holding guard (a risk exit).
  NEVER →  "alpha scored LOW", "a superior opportunity exists", any LLM re-rank or
           qualitative thesis re-read — those are rebalance-day decisions only.
  NOT YET → machine-checkable quantitative ``invalidates_if`` triggers: the journal
           stores qualitative text, not structured price levels; regex-mining them
           was explicitly rejected (Jun 13 — a commodity price or consensus figure
           misreads as a stock stop). Wire this only when a structured ``price_stop``
           field exists at entry.

Cross-mode SELL interlock (§6.5.3): a ticker the rebalance already traded this ISO
week (from ``last_rebalance.json``) is NEVER sold here — a Wednesday rebalance SELL
and a Thursday stop on the same name's residual would double-sell. An interlocked
stop is surfaced as DEGRADED health for human review instead.

Kill switch: checked daily here (it was previously checked by the daily main.py run;
with main.py weekly, risk_watch keeps peak-tracking and the drawdown alarm daily).
The kill switch blocks BUYs — risk_watch has none — so it is a health signal here,
never a forced liquidation (IPS: block new BUYs, not sell the book).

ZERO LLM · ZERO BUY (structural: only SELL decisions are constructible below).
"""

from __future__ import annotations

import json as _json
import uuid as _uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from execute import (get_portfolio_summary, execute_trades, log_trades,
                     order_executed, StalePortfolioError, BLOCKED_TICKERS, DRY_RUN)
from journal import (check_kill_switches, record_transaction, record_trade,
                     close_position, mark_execution_started, mark_pending_executed,
                     _load, LAST_REBALANCE_FILE)
from market_calendar import iso_week_of
from policy import get as _policy_get, policy_version as _policy_version
from health import HealthTracker, OK, DEGRADED, FAILED, ABORTED

_ET = ZoneInfo("America/New_York")

STOP_PCT = float(_policy_get("single_name_stop_pct", 0.25))


def _interlocked_tickers(today: str) -> set[str]:
    """Tickers the rebalance already traded this ISO week — never SELL them here."""
    lr = _load(LAST_REBALANCE_FILE, {})
    if not isinstance(lr, dict) or lr.get("iso_week") != iso_week_of(today):
        return set()
    return {t for t in (lr.get("tickers") or []) if isinstance(t, str)}


def evaluate_triggers(portfolio: dict, today: str,
                      stop_pct: float = STOP_PCT,
                      interlocked: set[str] | None = None) -> tuple[list[dict], dict]:
    """Evaluate the §6.7 trigger set against live positions. Pure + deterministic.

    Returns (decisions, report). Decisions are FULL-EXIT SELLs only — this function
    is structurally incapable of emitting a BUY. `report` records every position
    evaluated, which triggers fired, and which fired-but-interlocked (for health).
    """
    interlocked = interlocked if interlocked is not None else set()
    decisions: list[dict] = []
    report = {"evaluated": 0, "fired": [], "interlocked": [], "blocked": [], "skipped_no_basis": []}

    for p in portfolio.get("positions", []):
        ticker = p.get("symbol")
        if not ticker:
            continue
        report["evaluated"] += 1
        try:
            avg = float(p.get("avg_price") or 0)
            cur = float(p.get("current_price") or 0)
        except (TypeError, ValueError):
            avg = cur = 0.0
        if avg <= 0 or cur <= 0:
            # No provable cost basis / live price → cannot evaluate the stop. Never
            # sell on unverifiable data — surface instead (a manually-transferred
            # position, or an MCP field gap).
            report["skipped_no_basis"].append(ticker)
            continue

        drawdown = (cur - avg) / avg          # negative = loss vs entry
        if drawdown > -stop_pct:
            continue                          # stop not breached — the only trigger

        detail = {"ticker": ticker, "avg_price": round(avg, 4),
                  "current_price": round(cur, 4), "drawdown_pct": round(drawdown * 100, 2)}
        if ticker in BLOCKED_TICKERS:
            report["blocked"].append(detail)   # defense in depth — never tradeable
            continue
        if ticker in interlocked:
            # Fired but the rebalance traded this name this ISO week — a second
            # SELL could target the residual of the same position (§6.5.3).
            report["interlocked"].append(detail)
            continue

        report["fired"].append(detail)
        qty = float(p.get("available_qty", p.get("qty", 0)) or 0)
        decisions.append({
            "ticker":            ticker,
            "action":            "SELL",                       # the ONLY constructible action
            "target_weight":     0.0,                          # full exit
            "qty":               round(qty, 6),
            "source_of_capital": "risk_exit_stop_loss",
            "risk_exit":         True,                         # exempt from min-hold (§6.7)
            "expected_return":   0.0,
            "rationale": (f"risk exit: {drawdown:.1%} from ${avg:.2f} cost basis breaches "
                          f"the -{stop_pct:.0%} hard stop (morning evaluation, live MCP quote)"),
        })

    return decisions, report


def run_risk_watch() -> None:
    print("\n" + "=" * 60)
    print("🛡️   AI INVESTOR — RISK-WATCH (SELL-only safety net)")
    print("=" * 60)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    today  = datetime.now(_ET).strftime("%Y-%m-%d")
    health = HealthTracker(run_id, today)

    # ── 1. Portfolio (live MCP data — the routine's STEP 1 wrote it fresh) ──────
    print("\n📊  Fetching portfolio...")
    try:
        portfolio = get_portfolio_summary()
    except StalePortfolioError as e:
        print(f"\n   🚨 RISK-WATCH ABORTED: {e}")
        health.record("portfolio", FAILED, message=str(e)[:300])
        health.record("risk_watch", ABORTED,
                      message="Aborted — mcp_portfolio.json is stale or undated. "
                              "Stops were NOT evaluated today.")
        _write_health(health)
        return
    print(f"   Cash: ${portfolio['cash']:,.2f} | Positions: {len(portfolio['positions'])} "
          f"| Total: ${portfolio['total_value']:,.2f}")
    if portfolio.get("total_value", 0) > 0:
        health.record("portfolio", OK, total_value=portfolio["total_value"],
                      positions=len(portfolio["positions"]))
    else:
        health.record("portfolio", FAILED,
                      message="Portfolio fetch returned zero total value")

    # ── 2. Kill switch (daily peak tracking + drawdown alarm) ───────────────────
    kill_active, kill_reason = check_kill_switches(portfolio)
    if kill_active:
        print(f"   🛑 KILL SWITCH ACTIVE: {kill_reason}")
        health.record("kill_switch", DEGRADED, message=kill_reason)
    else:
        health.record("kill_switch", OK)

    # ── 3. Trigger evaluation (deterministic; no LLM) ───────────────────────────
    interlocked = _interlocked_tickers(today)
    decisions, report = evaluate_triggers(portfolio, today, interlocked=interlocked)

    msg_bits = [f"{report['evaluated']} position(s) evaluated",
                f"{len(report['fired'])} stop(s) fired"]
    status = OK
    if report["interlocked"]:
        status = DEGRADED
        msg_bits.append(f"{len(report['interlocked'])} fired-but-INTERLOCKED "
                        "(rebalance traded this name this ISO week — manual review)")
    if report["skipped_no_basis"]:
        status = DEGRADED
        msg_bits.append(f"{len(report['skipped_no_basis'])} position(s) had no "
                        f"provable cost basis/price: {report['skipped_no_basis']}")
    health.record("risk_watch", status, message="; ".join(msg_bits), **report)
    print(f"   🔍 {'; '.join(msg_bits)}")

    # ── 4. Envelope — the SAME idempotency machinery as the rebalance ──────────
    # mode="risk_watch" keeps this envelope out of the once-per-ISO-week rebalance
    # lock (journal._mirror_rebalance_stamp ignores it) while the daily
    # date+executed_at idempotency applies unchanged.
    prices = {p["symbol"]: {"close": float(p.get("current_price") or 0)}
              for p in portfolio.get("positions", []) if p.get("symbol")}
    with open("pending_decisions.json", "w") as f:
        _json.dump({
            "run_id":               run_id,
            "date":                 today,
            "mode":                 "risk_watch",
            "generated_at":         datetime.now(timezone.utc).isoformat(),
            "policy_version":       _policy_version(),
            "data_quality":         None,   # no snapshot consumed — triggers are live-MCP-priced
            "execution_started_at": None,
            "executed_at":          None,
            "decisions":            decisions,
        }, f, indent=2)

    if not decisions:
        print("\n   ✅ No risk triggers fired — nothing to do.")
        _publish(portfolio, health)
        _write_health(health)
        print("=" * 60 + "\n")
        return

    for d in decisions:
        print(f"   → SELL {d['ticker']} qty={d['qty']} | {d['rationale']}")

    # ── 5. Execute + log through the EXISTING envelope machinery ───────────────
    # In the cloud DRY_RUN=true: no orders happen here — the routine's STEP 4
    # places the MCP orders, stamps, and reconciles, exactly as on a rebalance
    # day. Locally with DRY_RUN=false, mirror main.py: claim → orders → stamp.
    order_results: dict = {}
    if not DRY_RUN:
        mark_execution_started(run_id)
    try:
        order_results = execute_trades(decisions, portfolio, prices)
    except Exception as e:
        print(f"   ❌ Execution error: {e}")
        health.record("execution", FAILED, message=str(e)[:200], dry_run=DRY_RUN)

    executed = [d for d in decisions if order_executed(order_results.get(d["ticker"]))]
    failed = {t: r for t, r in order_results.items() if not order_executed(r)}
    if not DRY_RUN and executed:
        mark_pending_executed(run_id)
    if order_results and not failed:
        health.record("execution", OK, decisions=len(decisions),
                      executed=len(executed), dry_run=DRY_RUN)
    elif failed:
        health.record("execution", DEGRADED if executed else FAILED,
                      message=f"{len(failed)} of {len(order_results)} order(s) failed",
                      failed_orders={t: str(r)[:120] for t, r in failed.items()},
                      dry_run=DRY_RUN)

    # ── 6. Speculative logs (same triple as main.py; reconciled by the routine's
    #      mark_transactions_live against broker fills — dry_run rows never publish) ──
    log_trades(executed, portfolio, prices, strategy="risk_watch",
               broker_order_ids=order_results, run_id=run_id)
    for d in executed:
        ticker = d["ticker"]
        price = prices.get(ticker, {}).get("close", 0)
        record_transaction({
            "transaction_id":         str(_uuid.uuid4()),
            "run_id":                 run_id,
            "date":                   today,
            "timestamp":              datetime.now(timezone.utc).isoformat(),
            "ticker":                 ticker,
            "action":                 "SELL",
            "qty":                    d.get("qty"),
            "price":                  round(price, 4) if price else None,
            "total_value":            round((d.get("qty") or 0) * price, 2) if price else None,
            "target_weight":          0.0,
            "portfolio_value_before": portfolio["total_value"],
            "source_of_capital":      "risk_exit_stop_loss",
            "regime":                 "",
            "rationale":              d.get("rationale", ""),
            "broker_order_id":        (order_results.get(ticker) or {}).get("id")
                                      if isinstance(order_results.get(ticker), dict) else None,
            "dry_run":                DRY_RUN,
        })
        trade_id = record_trade(
            ticker=ticker, action="SELL", target_weight=0.0,
            thesis=d.get("rationale", ""), anti_thesis="", catalysts=[],
            confidence=10, expected_return=0.0,
            invalidates_if=[], run_id=run_id,
        )
        avg_price = next((float(p.get("avg_price", 0) or 0)
                          for p in portfolio["positions"] if p.get("symbol") == ticker), 0.0)
        closed_id = close_position(ticker=ticker, exit_price=price,
                                   avg_price=avg_price, full_exit=True, run_id=run_id)
        print(f"   📔 {trade_id} SELL {ticker}"
              + (f" · closed thesis {closed_id}" if closed_id else ""))

    _publish(portfolio, health)
    _write_health(health)
    print("\n✅  Risk-watch complete.")
    print("=" * 60 + "\n")


def _publish(portfolio: dict, health: HealthTracker) -> None:
    """Morning website snapshot on research days (same 9:45 freshness the daily
    cycle used to provide). Regime resolution falls back to the last assessed
    value inside publish.py; observational — never breaks the run."""
    try:
        from publish import publish_to_supabase
        from main import _record_supabase_health
        try:
            publish_to_supabase(portfolio)
            health.record("supabase_publish", OK)
        except Exception as e:
            _record_supabase_health(health, e)
            print(f"   ⚠ Supabase publish skipped: {e}")
    except Exception as e:
        print(f"   ⚠ publish unavailable: {e}")


def _write_health(health: HealthTracker) -> None:
    data = health.save()
    print(f"\n📋 system_health.json written — overall_status={data['overall_status']}")


if __name__ == "__main__":
    run_risk_watch()
