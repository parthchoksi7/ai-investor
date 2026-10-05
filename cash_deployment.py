"""
cash_deployment.py — the bounded mechanical re-deployment rule (IPS §6, v1.5, 2026-10-04).

The IPS cash target is 0–10%, but until now it lived only in the Portfolio Manager's
prompt: the book ran 13–63% cash for months (31.7% on 2026-10-02) while every control in
the system was a brake. Cash has beta 0, so idle cash silently capped the book's market
exposure — Gate 2 of SCALE_DECISION_RULE (book beta 0.8–1.2) was unreachable, and the
experiment was measuring "does a two-thirds-invested book beat SPY" rather than "can AI
pick stocks". The 2026-07-05 ratified exception said it would close with "either a bounded
mechanical re-deployment rule ... or the defensive posture made permanent". This is the rule.

WHAT IT DOES. After the CRO and BEFORE the guard chain (a PRODUCER, so every guard still
sees — and may clamp or drop — what it proposes), when the book's projected post-trade
cash exceeds `cash_deploy_target_pct`, it scales the AI's own intended book up
proportionally: every eligible name (today's approved BUYs and the holdings it keeps) is
multiplied by one common factor, each capped at the per-name maximum, until projected cash
reaches the target or today's settled budget runs out. Deployed capital therefore follows
the AI's selections and relative sizing; nothing new is picked here.

WHAT IT NEVER DOES. Sell anything. Buy a name the AI is selling, the CRO vetoed, the Devil's
Advocate flagged, Position Review wants reduced, the wash-sale guard would block, the
sector map cannot place, or that has no research thesis. Add to a holding that is already
half-way to its stop-loss (averaging down toward a catastrophe exit). Run on a CRO full veto
or outage, or while the kill switch is active. Spend unsettled sale proceeds (they are the
next week's deployment — PLAN_OCT4_REVIEW item 3).

KNOWN SIDE EFFECT (disclosed, accepted): a top-up BUY on a holding is a new lot, so the
min-hold guard (which keys on the LATEST buy) restarts that holding's 30/63-trading-day
discretionary-sell clock. Risk exits are unaffected. Consistent with the 9–12-month horizon.

Top-ups of holdings carry `cash_deployment: True` so measurement can tell them apart from
the AI's own selections (calibration.log_decisions excludes them from `pm_selected`).
"""

from __future__ import annotations

from guardrails import (MAX_TARGET_WEIGHT, MIN_ORDER_NOTIONAL, BLOCKED_TICKERS,
                        sector_of, settled_budget, wash_sale_days_remaining)
from policy import get as _policy_get

CASH_DEPLOY_TARGET_PCT = float(_policy_get("cash_deploy_target_pct", 0.05))
STOP_PCT               = float(_policy_get("single_name_stop_pct", 0.25))


def _ineligible_reason(t: str, pipeline_state: dict, sells: set, vetoed: set,
                       holding: dict | None, transactions: list, today: str) -> str | None:
    if t in sells:
        return "being sold"
    if t in vetoed:
        return "CRO veto"
    if t in BLOCKED_TICKERS:
        return "blocked ticker"
    if sector_of(t) == "UNKNOWN":
        return "unmapped sector"
    if ((pipeline_state.get("devils_advocate") or {}).get(t) or {}).get("recommend_reject"):
        return "Devil's Advocate reject"
    pr = (pipeline_state.get("position_reviews") or {}).get(t) or {}
    if pr.get("recommended_action") in ("REDUCE", "EXIT"):
        return f"position review {pr.get('recommended_action')}"
    if not str(((pipeline_state.get("research") or {}).get(t) or {}).get("thesis", "")).strip():
        return "no research thesis"
    if (wash_sale_days_remaining(t, transactions, today=today) or 0) > 0:
        return "wash-sale window"
    if holding:
        try:
            avg, cur = float(holding.get("avg_price") or 0), float(holding.get("current_price") or 0)
        except (TypeError, ValueError):
            avg = cur = 0.0
        if avg > 0 and cur > 0 and (cur - avg) / avg <= -STOP_PCT / 2:
            return f"{(cur - avg) / avg:.0%} from cost — half-way to the stop"
    return None


def plan_cash_deployment(decisions: list, portfolio: dict, prices: dict,
                         pipeline_state: dict, transactions: list, today: str,
                         kill_active: bool = False,
                         target_cash_pct: float = CASH_DEPLOY_TARGET_PCT,
                         max_weight: float = MAX_TARGET_WEIGHT,
                         min_notional: float = MIN_ORDER_NOTIONAL) -> tuple[list, dict]:
    """Return (decisions, report). Reads only its arguments; for unusable inputs it
    returns `decisions` unchanged with a `skipped` reason rather than raising."""
    report: dict = {"deployed_usd": 0.0, "names": {}, "excluded": {}, "skipped": None,
                    "target_cash_pct": target_cash_pct}
    try:
        tv = float(portfolio.get("total_value") or 0)
    except (TypeError, ValueError):
        tv = 0.0
    cro = pipeline_state.get("cro") or {}
    if tv <= 0:
        report["skipped"] = "no portfolio value"
        return decisions, report
    if kill_active:
        report["skipped"] = "kill switch active"
        return decisions, report
    if cro.get("api_failed"):
        report["skipped"] = "CRO call failed — no deployment without a risk review"
        return decisions, report
    vetoed = set(cro.get("rejected_tickers") or [])
    if not cro.get("approved", True) and not vetoed:
        report["skipped"] = "CRO full veto"
        return decisions, report

    holdings = {p.get("symbol"): p for p in portfolio.get("positions", []) if p.get("symbol")}
    weight = {t: float(p.get("market_value") or 0) / tv for t, p in holdings.items()}
    sells = {d.get("ticker") for d in decisions if str(d.get("action", "")).upper() == "SELL"}
    buy_dec = {d.get("ticker"): d for d in decisions if str(d.get("action", "")).upper() == "BUY"}
    planned_buy_usd = 0.0
    for d in decisions:
        a, t = str(d.get("action", "")).upper(), d.get("ticker")
        try:
            tw = float(d.get("target_weight") or 0)
        except (TypeError, ValueError):
            continue
        if a == "SELL":
            weight[t] = min(weight.get(t, 0.0), tw)
        elif a == "BUY":
            planned_buy_usd += max(0.0, tw - weight.get(t, 0.0)) * tv
            weight[t] = max(weight.get(t, 0.0), tw)

    projected_cash = 1.0 - sum(weight.values())
    report["projected_cash_pct"] = round(projected_cash, 4)
    excess = projected_cash - target_cash_pct
    if excess <= 1e-9:
        report["skipped"] = f"projected cash {projected_cash:.1%} within target"
        return decisions, report
    budget, _avail, src = settled_budget(portfolio)
    spendable = budget - planned_buy_usd
    report["settled_budget_usd"] = round(budget, 2)
    if spendable < min_notional:
        report["skipped"] = (f"settled cash exhausted by today's BUYs ({src}); the excess "
                             "deploys next rebalance")
        return decisions, report
    to_deploy = min(excess * tv, spendable)

    eligible = {}
    for t, w in weight.items():
        if w <= 0:
            continue
        why = _ineligible_reason(t, pipeline_state, sells, vetoed, holdings.get(t),
                                 transactions, today)
        if why:
            report["excluded"][t] = why
        elif w < max_weight - 1e-9 and (prices.get(t) or {}).get("close"):
            eligible[t] = w
    if not eligible:
        report["skipped"] = "no eligible name to deploy into"
        return decisions, report

    # Water-fill: one common multiplier, each name capped at max_weight.
    new_w = dict(eligible)
    remaining = to_deploy / tv
    active = set(eligible)
    while remaining > 1e-9 and active:
        base = sum(new_w[t] for t in active)
        factor = 1 + remaining / base
        capped = {t for t in active if new_w[t] * factor >= max_weight}
        if not capped:
            for t in active:
                new_w[t] *= factor
            remaining = 0.0
            break
        for t in capped:
            remaining -= max_weight - new_w[t]
            new_w[t] = max_weight
        active -= capped

    # Enlarged PM BUYs are REPLACED by copies, never mutated: the same dict objects are
    # recorded as the agents' intent (post_cro_decisions / portfolio_manager_proposed),
    # and an in-place edit would rewrite that audit record (the Aug 19 2026 ultra
    # finding about shared decision objects).
    raised: dict = {}
    added: list = []
    for t in sorted(eligible):
        add_usd = (new_w[t] - eligible[t]) * tv
        if add_usd < min_notional:
            continue
        tw = round(new_w[t], 4)
        if t in buy_dec:
            d = buy_dec[t]
            raised[t] = {**d, "target_weight": tw,
                         "cash_deployment_added_weight":
                             round(tw - float(d.get("target_weight") or 0), 4)}
        else:
            added.append({"ticker": t, "action": "BUY", "target_weight": tw,
                        "source_of_capital": "cash_deployment", "cash_deployment": True,
                        "rationale": (f"Mechanical cash deployment (IPS §6): projected cash "
                                      f"{projected_cash:.1%} > {target_cash_pct:.0%} target — "
                                      f"holding scaled with the rest of the book "
                                      f"{eligible[t]:.1%} → {tw:.1%}")})
        report["names"][t] = {"from": round(eligible[t], 4), "to": tw, "add_usd": round(add_usd, 2)}
        report["deployed_usd"] += add_usd
    out = [raised.get(d.get("ticker"), d) if str(d.get("action", "")).upper() == "BUY" else d
           for d in decisions] + added
    report["deployed_usd"] = round(report["deployed_usd"], 2)
    if report["names"]:
        print(f"   💵 CASH DEPLOYMENT: ${report['deployed_usd']:.2f} across "
              f"{sorted(report['names'])} (projected cash {projected_cash:.1%} → target "
              f"{target_cash_pct:.0%})")
    return out, report
