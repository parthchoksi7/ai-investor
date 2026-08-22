"""
core_builder.py — Phase 3 of the beta/alpha split: the deterministic core producer.

This is a PRODUCER, not a guard. It proposes decisions; it does not veto them. That
distinction is structural, not stylistic (PLAN_BETA_ALPHA_SPLIT.md §3):

    Every guard in the chain may reject or clamp a decision. None may create one.

All eight guards in `guardrails.py` are strictly subtractive, which is what makes the
chain safe to reason about — it can only ever move a run toward FEWER orders. A floor
(minimum exposure, minimum holdings, a cash ceiling) cannot live in a guard without
inverting the failure direction from missed trades to unintended ones. So the beta
band's UPPER bound is enforced downstream as a guard, and its LOWER bound is enforced
here, upstream, where it is visible to every guard that follows.

Two SEPARATE controls, deliberately not collapsed into one
──────────────────────────────────────────────────────────
`TARGET_INVESTED_PCT` (how much capital is deployed) and `BETA_LO/BETA_HI` (how much
market exposure it carries) are independent parameters. It is tempting to fold the
first into the second — cash carries beta 0, so a beta floor is arithmetically a cash
ceiling — but that is exactly the conflation this whole plan exists to undo. It also
does not work at these levels: post-neutralization the universe mean `beta_stable` is
~0.82, so a 0.60 beta floor alone permits ~27% cash, MORE than the 18.1% the book
already carries. The deployment target has to be its own control or the cash-drag
problem survives the fix.

Zero order code. This module emits decision dicts in the same shape the Portfolio
Manager emits them; they flow through the identical guard chain, `_compute_qty`,
`pending_decisions.json`, the claim/stamp protocol and `mark_transactions_live`.
"""

from __future__ import annotations

import json
import os
from datetime import date

import policy

CORE_STATE_FILE = "core_holdings.json"

# Selection/sizing knobs. Sourced from policy.yaml where a governed limit exists, so a
# value is never defined in two places (IPS §18.4 single-source rule).
DEFAULT_N_HOLDINGS = 13

# Deployment target — the share of the book the core aims to hold in equities. The
# residual is the working cash buffer; the IPS band is 0–10%.
TARGET_INVESTED_PCT = 0.97

# Portfolio beta band, measured on `beta_stable` with CASH COUNTED AT BETA 0.
BETA_LO = 0.60
BETA_HI = 0.80

# Selection steers toward the MIDPOINT rather than merely "somewhere in the band" —
# a book parked on a boundary is one price move from breaching it, and every breach
# costs a rebalance the min-hold and tax rules would rather not pay for.
BETA_TARGET = (BETA_LO + BETA_HI) / 2

# A swap is only worth making if it moves realized beta toward the target by at least
# this much; below it the steering loop is churning on noise.
_MIN_BETA_IMPROVEMENT = 1e-4

# Reconstitution cadence. The core is a buy-and-hold sleeve: it is NOT re-selected on
# rank drift, because turnover is the measured enemy here (100% of realized lots to
# date are short-term, taxed ~54%). It reconstitutes annually, or when the band has
# been breached for `BREACH_PATIENCE` consecutive rebalances.
RECONSTITUTE_AFTER_DAYS = 365
BREACH_PATIENCE = 2


# ── state ────────────────────────────────────────────────────────────────────

def load_core_state(path: str = CORE_STATE_FILE) -> dict:
    """Which tickers are core, and when they were constituted.

    Missing/corrupt → an empty state, which reads as "no core yet" and triggers an
    initial construction. Never raises: a research-side file must not be able to
    abort a trading run.
    """
    if os.path.isfile(path):
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("tickers"), list):
                return d
        except Exception:
            pass
    return {"tickers": [], "constituted": None, "breach_streak": 0}


def save_core_state(state: dict, path: str = CORE_STATE_FILE) -> None:
    """Atomic write — a crash mid-write must not leave an unparseable core state."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ── selection ────────────────────────────────────────────────────────────────

def _eligible(scores: dict, exclude: set[str]) -> list[tuple[str, dict]]:
    """Names that can carry a core position: scoreable, and with a beta estimate
    trustworthy enough to size on.

    `beta_stable_available` is deliberately strict — it means the LONG-basis estimate
    (see quant_engine). A 22–119 session beta is shorter than the 63-session estimate
    this whole plan exists to condemn, and must never reach position sizing.
    """
    blocked = set(policy.get("blocked_tickers", []) or [])
    out = []
    for t, s in scores.items():
        if t in exclude or t in blocked or t in ("SPY", "QQQ"):
            continue
        if not (s.get("data_available") and s.get("momentum_available")):
            continue
        if not s.get("beta_stable_available"):
            continue
        if not isinstance(s.get("composite_score"), (int, float)):
            continue
        out.append((t, s))
    return out


def select_core(scores: dict,
                n_holdings: int = DEFAULT_N_HOLDINGS,
                beta_lo: float = BETA_LO,
                beta_hi: float = BETA_HI,
                exclude: set[str] | None = None,
                target_invested: float = TARGET_INVESTED_PCT,
                max_swaps: int = 500) -> dict:
    """Choose the core basket: best-scoring names, steered into the beta band.

    Selection is a two-step, and the order matters. It takes the top `n_holdings` by
    composite score FIRST, then swaps the minimum number of names needed to bring the
    equal-weighted mean `beta_stable` to the band. Steering second keeps the ranking
    primary and the beta constraint a correction — the reverse (filter to a beta band,
    then rank within it) is a per-name beta SCREEN, which was the worst arm ever
    measured in this harness: −7.93% after-tax alpha at 5.27× turnover, because a hard
    cutoff amplifies estimation error at the boundary.

    The band is measured on the PORTFOLIO beta — the names' mean scaled by
    `target_invested`, with the uninvested residual counted at beta 0 — not on the
    names' mean alone. Those differ, and steering the wrong one lets the book sit
    outside the band while every name looks compliant: at 97% invested a names-mean
    of 0.60 is a portfolio beta of 0.58.

    Returns {"tickers", "mean_beta", "portfolio_beta", "in_band", "swaps",
    "worst_rank", "eligible"}. Empty `tickers` means the universe could not support
    a core at all.
    """
    ranked = sorted(_eligible(scores, exclude or set()),
                    key=lambda x: x[1]["composite_score"], reverse=True)
    if len(ranked) < n_holdings:
        return {"tickers": [], "mean_beta": None, "portfolio_beta": None,
                "in_band": False, "swaps": 0, "worst_rank": None,
                "eligible": len(ranked),
                "reason": f"only {len(ranked)} eligible names, need {n_holdings}"}

    rank_of = {t: i for i, (t, _) in enumerate(ranked)}
    beta_of = {t: s["beta_stable"] for t, s in ranked}
    chosen = [t for t, _ in ranked[:n_holdings]]
    pool = [t for t, _ in ranked[n_holdings:]]

    def mean_beta(names):
        return sum(beta_of[t] for t in names) / len(names)

    def port_beta(names):
        """The number the band actually governs: cash contributes beta 0."""
        return mean_beta(names) * target_invested

    target_port = (beta_lo + beta_hi) / 2

    swaps = 0
    for _ in range(max_swaps):
        mb = port_beta(chosen)
        if beta_lo <= mb <= beta_hi:
            break
        # Swap out the chosen name furthest from target in the offending direction,
        # for the BEST-SCORING pool name that moves us back — so the rank cost of
        # steering is paid as cheaply as possible.
        too_high = mb > beta_hi
        worst = max(chosen, key=lambda t: beta_of[t]) if too_high \
            else min(chosen, key=lambda t: beta_of[t])
        candidates = [t for t in pool
                      if (beta_of[t] < beta_of[worst]) == too_high]
        if not candidates:
            break
        best_in = min(candidates, key=lambda t: rank_of[t])
        trial = [t for t in chosen if t != worst] + [best_in]
        if abs(port_beta(trial) - target_port) >= abs(mb - target_port) - _MIN_BETA_IMPROVEMENT:
            break                      # no meaningful progress — stop churning
        pool.remove(best_in)
        pool.append(worst)
        chosen = trial
        swaps += 1

    pb = port_beta(chosen)
    chosen.sort(key=lambda t: rank_of[t])
    return {"tickers": chosen,
            "mean_beta": round(mean_beta(chosen), 4),
            "portfolio_beta": round(pb, 4),
            "in_band": beta_lo <= pb <= beta_hi,
            "swaps": swaps,
            "worst_rank": max(rank_of[t] for t in chosen) + 1,
            "eligible": len(ranked)}


# ── portfolio-level beta, the number the band is measured on ─────────────────

def portfolio_beta(weights: dict, betas: dict) -> float:
    """Weighted mean beta with CASH COUNTED AT BETA 0.

    Counting cash is the whole point. A book 36% in cash cannot exceed 0.64 beta even
    if every stock it holds has beta 1.0 — which is precisely how idle cash became an
    unchosen market call here (−0.14 portfolio beta while `cash_discipline_status`
    had been DEGRADED for 29 consecutive runs without gating anything). `weights` are
    fractions OF TOTAL VALUE, so any shortfall from 1.0 is cash and contributes zero.
    """
    return sum(w * betas.get(t, 0.0) for t, w in weights.items())


# ── reconstitution ───────────────────────────────────────────────────────────

def should_reconstitute(state: dict, today: date | None = None,
                        band_breached: bool = False) -> tuple[bool, str]:
    """Is the core due for re-selection?

    Deliberately conservative. The core is buy-and-hold: re-selecting it on rank drift
    would reintroduce exactly the turnover this plan exists to remove (every realized
    lot to date is short-term, avg holding 11.7 days, taxed ~54%).
    """
    today = today or date.today()
    if not state.get("tickers"):
        return True, "no core constituted yet"
    constituted = state.get("constituted")
    if constituted:
        try:
            age = (today - date.fromisoformat(constituted)).days
            if age >= RECONSTITUTE_AFTER_DAYS:
                return True, f"annual reconstitution ({age}d since {constituted})"
        except (ValueError, TypeError):
            return True, "unparseable constitution date"
    else:
        return True, "no constitution date recorded"
    if band_breached and state.get("breach_streak", 0) + 1 >= BREACH_PATIENCE:
        return True, (f"beta band breached {state.get('breach_streak', 0) + 1} "
                      f"consecutive rebalances")
    return False, "core intact"


# ── decisions ────────────────────────────────────────────────────────────────

def plan_core_trades(target_tickers: list[str],
                     portfolio: dict,
                     scores: dict,
                     target_invested: float = TARGET_INVESTED_PCT,
                     max_weight: float | None = None) -> list[dict]:
    """Turn a target core basket into decision dicts, in the PM's own output shape.

    Emits only what is needed to reach the target: BUYs for names not held or held
    under weight, SELLs for core names being dropped. Names the core does not mention
    are left entirely alone — they belong to the sleeve, and one layer must never
    silently trade another's position (the ticker-disjointness rule, §3).
    """
    if max_weight is None:
        max_weight = float(policy.get("max_target_weight", 0.10))
    if not target_tickers:
        return []

    weight = min(max_weight, target_invested / len(target_tickers))
    held = {p["symbol"]: float(p.get("qty") or 0) for p in portfolio.get("positions", [])}
    total = float(portfolio.get("total_value") or 0)

    decisions: list[dict] = []
    for t in target_tickers:
        cur_w = 0.0
        if total:
            for p in portfolio.get("positions", []):
                if p["symbol"] == t:
                    cur_w = float(p.get("market_value") or 0) / total
                    break
        if cur_w >= weight - 1e-6:
            continue                                  # already at or above target
        s = scores.get(t, {})
        decisions.append({
            "ticker": t,
            "action": "BUY",
            "target_weight": round(weight, 6),
            "source_of_capital": "cash",
            "layer": "core",
            "expected_return": None,
            "rationale": (f"core basket, equal weight; composite "
                          f"{s.get('composite_score')}, beta_stable "
                          f"{s.get('beta_stable')}"),
        })
    return decisions


def summarize(selection: dict, decisions: list[dict]) -> str:
    """One-line human summary for the run log."""
    if not selection.get("tickers"):
        return f"core: NOT CONSTITUTED — {selection.get('reason', 'unknown')}"
    return (f"core: {len(selection['tickers'])} names, portfolio beta "
            f"{selection['portfolio_beta']:+.3f} (names mean "
            f"{selection['mean_beta']:+.3f}) "
            f"({'in band' if selection['in_band'] else '⚠ OUT OF BAND'}), "
            f"{selection['swaps']} beta swap(s), worst rank "
            f"{selection['worst_rank']}/{selection['eligible']}, "
            f"{len(decisions)} trade(s)")
