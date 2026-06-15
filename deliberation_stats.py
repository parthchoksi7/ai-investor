"""
deliberation_stats.py — behavioral + operational base rates (B14, B16).

Section 5 of PAPER_DRAFT reported "several occasions", "some names" etc. WITHOUT
denominators — exactly the denominator-free anecdote the paper criticizes
elsewhere. This module converts that into measured base rates from data already
on disk, requiring NO new market data and NO return history:

  deliberation_stats()  (B14) — behavior of the pipeline from agent_log.json:
      CRO veto rate, Devil's-Advocate reject rate, PM trade/no-trade rate,
      DA-flag ↔ PM-no-buy coincidence, bull/bear disagreement, regime mix.

  operational_stats()   (B16) — operational base rates + turnover from
      agent_log.json + transactions.json: run/trade counts, no-trade-run rate,
      kill-switch activations, realized turnover, and the ST/LT holding-period
      split that de-assumes PAPER_DRAFT §6.6's worst-case short-term tax bracket.

Both are PURE over their inputs (inject the loaded log/txns for tests). Every
output carries its denominator `n`; with a short deployment these are small and
must be read as base rates with wide error, not conclusions. Honest plumbing,
not proof.

Not derivable here (documented gaps, not silent omissions):
  - Per-agent token/cost: agent_log.json does not record token usage. This lands
    with the A12 reproducibility logging (resolved model id + usage per call).
  - Abort/skip/uptime counts: system_health.json is overwritten each run (no
    retained history), so aborted runs leave no row in agent_log. Only completed
    runs are counted; the abort denominator needs health-history retention.
"""

import json
import os
from collections import Counter

AGENT_LOG    = "agent_log.json"
TRANSACTIONS = "transactions.json"
REPORT       = "deliberation_stats.json"


def _rate(num: int, den: int):
    return round(num / den, 4) if den else None


def deliberation_stats(log: list) -> dict:
    """B14 — behavioral base rates from a list of agent_log runs."""
    runs = [r for r in (log or []) if isinstance(r, dict)]
    n_runs = len(runs)

    # ── CRO ──────────────────────────────────────────────────────────────────
    cro_full_vetoes = sum(1 for r in runs if (r.get("cro") or {}).get("approved") is False)
    cro_partial = sum(1 for r in runs if (r.get("cro") or {}).get("rejected_tickers"))
    cro_rejected_names = sum(len((r.get("cro") or {}).get("rejected_tickers") or []) for r in runs)

    # ── Devil's Advocate (per ticker, across runs) ─────────────────────────────
    da_eval = da_reject = 0
    for r in runs:
        for _t, v in (r.get("devils_advocate") or {}).items():
            rj = (v or {}).get("recommend_reject")
            if isinstance(rj, bool):
                da_eval += 1
                da_reject += 1 if rj else 0

    # ── Portfolio Manager trade rate (proposed trades vs candidate breadth) ────
    cand_total = sum(len(r.get("candidates") or []) for r in runs)
    pm_actions = Counter()
    pm_proposed_total = 0
    for r in runs:
        for d in (r.get("portfolio_manager_proposed") or []):
            a = str(d.get("action", "")).upper()
            if a in ("BUY", "SELL"):
                pm_actions[a] += 1
                pm_proposed_total += 1

    # ── DA flag ↔ PM no-buy coincidence ────────────────────────────────────────
    da_flagged = pm_did_not_buy = 0
    for r in runs:
        bought = {str(d.get("ticker")) for d in (r.get("portfolio_manager_proposed") or [])
                  if str(d.get("action", "")).upper() == "BUY"}
        for t, v in (r.get("devils_advocate") or {}).items():
            if (v or {}).get("recommend_reject") is True:
                da_flagged += 1
                if t not in bought:
                    pm_did_not_buy += 1

    # ── Bull/bear disagreement: high research confidence AND a DA reject ───────
    conflict_pairs = conflicts = 0
    for r in runs:
        research = r.get("research") or {}
        da = r.get("devils_advocate") or {}
        for t, rv in research.items():
            conf = (rv or {}).get("confidence")
            rj   = (da.get(t) or {}).get("recommend_reject")
            if isinstance(conf, (int, float)) and isinstance(rj, bool):
                conflict_pairs += 1
                if conf >= 7 and rj:
                    conflicts += 1

    # ── Position-review REDUCE/EXIT rate ───────────────────────────────────────
    pr_total = pr_reduce_exit = 0
    for r in runs:
        for _t, v in (r.get("position_reviews") or {}).items():
            pr_total += 1
            if (v or {}).get("recommended_action") in ("REDUCE", "EXIT"):
                pr_reduce_exit += 1

    regime_mix = Counter((r.get("regime") or {}).get("regime") for r in runs if r.get("regime"))

    return {
        "n_runs": n_runs,
        "cro": {
            "full_veto_rate":      _rate(cro_full_vetoes, n_runs),
            "full_vetoes":         cro_full_vetoes,
            "partial_veto_runs":   cro_partial,
            "rejected_names_total": cro_rejected_names,
        },
        "devils_advocate": {
            "n_evaluated":  da_eval,
            "reject_rate":  _rate(da_reject, da_eval),
            "rejects":      da_reject,
        },
        "portfolio_manager": {
            "candidates_total":   cand_total,
            "trades_proposed":    pm_proposed_total,
            "trade_rate_vs_candidates": _rate(pm_proposed_total, cand_total),
            "no_trade_rate_vs_candidates": (round(1 - pm_proposed_total / cand_total, 4)
                                            if cand_total else None),
            "buy": pm_actions.get("BUY", 0), "sell": pm_actions.get("SELL", 0),
        },
        "da_flag_pm_no_buy": {
            "da_flagged":   da_flagged,
            "pm_no_buy":    pm_did_not_buy,
            "coincidence_rate": _rate(pm_did_not_buy, da_flagged),
        },
        "bull_bear_conflict": {
            "pairs":    conflict_pairs,
            "conflicts": conflicts,
            "rate":     _rate(conflicts, conflict_pairs),
            "note": "research confidence ≥ 7 AND Devil's-Advocate recommend_reject=true",
        },
        "position_review": {
            "n": pr_total,
            "reduce_exit_rate": _rate(pr_reduce_exit, pr_total),
        },
        "regime_mix": dict(regime_mix),
        "caveat": (f"n={n_runs} runs — base rates with wide error, not conclusions. "
                   "Per-agent token/cost not logged (see A12)."),
    }


def operational_stats(log: list, transactions: list,
                      health: dict | None = None) -> dict:
    """B16 — operational base rates + realized turnover / holding-period split."""
    runs = [r for r in (log or []) if isinstance(r, dict)]
    n_runs = len(runs)
    dates = sorted(r.get("date") for r in runs if r.get("date"))

    # Executed trades (post-guardrail final decisions).
    trade_runs = no_trade_runs = total_trades = buys = sells = 0
    for r in runs:
        fd = [d for d in (r.get("final_decisions") or [])
              if str(d.get("action", "")).upper() in ("BUY", "SELL")]
        total_trades += len(fd)
        buys  += sum(1 for d in fd if str(d.get("action")).upper() == "BUY")
        sells += sum(1 for d in fd if str(d.get("action")).upper() == "SELL")
        if fd:
            trade_runs += 1
        else:
            no_trade_runs += 1
    kill_runs = sum(1 for r in runs if r.get("kill_switch_active"))

    # ── Turnover + holding-period split (de-assumes §6.6 worst case) ───────────
    from performance import compute_realized_lots
    realized, uncovered = compute_realized_lots(transactions)
    st = [l for l in realized if l.get("term") == "ST"]
    lt = [l for l in realized if l.get("term") == "LT"]
    n_realized = len(realized)
    avg_hold = (round(sum(l.get("holding_days", 0) for l in realized) / n_realized, 1)
                if n_realized else None)

    # Sell notional ÷ average book value = period turnover (one-way, sells).
    sell_notional = sum(float(t.get("qty") or 0) * float(t.get("price") or 0)
                        for t in (transactions or [])
                        if str(t.get("action", "")).upper() == "SELL" and not t.get("dry_run"))
    tvs = [float((r.get("portfolio_snapshot") or {}).get("total_value") or 0)
           for r in runs if (r.get("portfolio_snapshot") or {}).get("total_value")]
    avg_book = sum(tvs) / len(tvs) if tvs else 0
    turnover = round(sell_notional / avg_book, 4) if avg_book else None

    return {
        "window": {"first": dates[0] if dates else None,
                   "last": dates[-1] if dates else None, "n_runs": n_runs},
        "trades": {
            "total": total_trades, "buys": buys, "sells": sells,
            "trades_per_run": round(total_trades / n_runs, 3) if n_runs else None,
            "trade_run_rate": _rate(trade_runs, n_runs),
            "no_trade_run_rate": _rate(no_trade_runs, n_runs),
        },
        "kill_switch_active_runs": kill_runs,
        "turnover": {
            "sell_notional": round(sell_notional, 2),
            "avg_book_value": round(avg_book, 2),
            "period_turnover_oneway": turnover,
            "note": "sell notional ÷ average book value over the window (one-way).",
        },
        "holding_period": {
            "n_realized_lots": n_realized,
            "short_term_lots": len(st),
            "long_term_lots":  len(lt),
            "short_term_share": _rate(len(st), n_realized),
            "avg_holding_days": avg_hold,
            "uncovered_sells": len(uncovered),
            "note": ("ST/LT split of realized round-trips — replaces §6.6's worst-case "
                     "all-short-term assumption with the measured number. Uncovered "
                     "SELLs (no in-log basis) are excluded, not guessed."),
        },
        "health_current": (health or {}).get("status") if health else None,
        "caveat": ("Only COMPLETED runs appear in agent_log; aborted/skipped runs leave "
                   "no row (system_health.json is overwritten each run), so abort-rate "
                   "and uptime need health-history retention before they can be reported."),
    }


def _load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def build(agent_log_path: str = AGENT_LOG, transactions_path: str = TRANSACTIONS,
          health_path: str = "system_health.json") -> dict:
    log  = _load_json(agent_log_path, [])
    txns = _load_json(transactions_path, [])
    health = _load_json(health_path, {})
    return {"deliberation": deliberation_stats(log),
            "operational":  operational_stats(log, txns, health)}


def main() -> None:
    out = build()
    with open(REPORT, "w") as f:
        json.dump(out, f, indent=2)
    d, o = out["deliberation"], out["operational"]
    print("\n" + "=" * 64)
    print(f"🧠  DELIBERATION BASE RATES  (B14)   n={d['n_runs']} runs")
    print("=" * 64)
    print(f"   CRO full-veto rate:        {d['cro']['full_veto_rate']}  "
          f"({d['cro']['full_vetoes']}/{d['n_runs']} runs); "
          f"partial-veto runs: {d['cro']['partial_veto_runs']}")
    print(f"   Devil's-Advocate reject:   {d['devils_advocate']['reject_rate']}  "
          f"({d['devils_advocate']['rejects']}/{d['devils_advocate']['n_evaluated']} tickers)")
    print(f"   PM trade rate / candidate: {d['portfolio_manager']['trade_rate_vs_candidates']}  "
          f"(buys {d['portfolio_manager']['buy']}, sells {d['portfolio_manager']['sell']})")
    print(f"   DA-flag ↔ PM no-buy:       {d['da_flag_pm_no_buy']['coincidence_rate']}  "
          f"({d['da_flag_pm_no_buy']['pm_no_buy']}/{d['da_flag_pm_no_buy']['da_flagged']})")
    print(f"   Bull/bear conflict rate:   {d['bull_bear_conflict']['rate']}  "
          f"({d['bull_bear_conflict']['conflicts']}/{d['bull_bear_conflict']['pairs']} pairs)")
    print(f"   Position REDUCE/EXIT rate: {d['position_review']['reduce_exit_rate']}")
    print(f"   Regime mix: {d['regime_mix']}")
    print("\n" + "=" * 64)
    print(f"⚙️   OPERATIONAL BASE RATES  (B16)   {o['window']['n_runs']} runs "
          f"({o['window']['first']} → {o['window']['last']})")
    print("=" * 64)
    t = o["trades"]
    print(f"   Trades: {t['total']} ({t['buys']} buy / {t['sells']} sell), "
          f"{t['trades_per_run']}/run; no-trade-run rate {t['no_trade_run_rate']}")
    print(f"   Kill-switch-active runs:   {o['kill_switch_active_runs']}")
    hp = o["holding_period"]
    print(f"   Realized round-trips: {hp['n_realized_lots']} "
          f"(ST {hp['short_term_lots']} / LT {hp['long_term_lots']}; "
          f"ST share {hp['short_term_share']}), avg hold {hp['avg_holding_days']}d, "
          f"{hp['uncovered_sells']} uncovered")
    print(f"   Period turnover (1-way):   {o['turnover']['period_turnover_oneway']}")
    print(f"\n   ⚠ {o['caveat']}")
    print("=" * 64 + "\n")
    print(f"   📄 Written to {REPORT}")


if __name__ == "__main__":
    main()
