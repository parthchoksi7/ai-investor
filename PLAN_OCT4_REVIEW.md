# Oct 4 2026 end-to-end review — remediation plan

Source: the 2026-10-04 nine-seat review (senior_backend, quant, PM, CRO, tax, data, IPS,
ML/AI, platform). Deadline for anything that changes a decision: **before the Wed
2026-10-07 rebalance**, when the `SCALE_DECISION_RULE.md` §7 freeze begins. Measurement-only
items are allowed under the freeze but must be dated in §7.

Status: ⬜ not started · 🟡 in progress · ✅ done · ⏸ owner decision

| # | Item | Changes decisions? | Status |
|---|------|--------------------|--------|
| 1 | Evaluate the −25% stop on rebalance days too (`main.py` injects code-built risk-exit SELLs after the CRO, before the guards); strip any LLM-supplied `risk_exit`; min-hold + circuit breaker exempt risk exits | **yes** — ship before Wed | ✅ built + tested (PR pending merge) |
| 2 | Cash-deployment producer (deploy cash above the 10% band at the rebalance) | yes | 🟡 owner approved 2026-10-04 — build blocked by the auto-mode permission check (code that creates BUY orders); awaiting owner go-ahead |
| 3 | Rotation proceeds idle a week (cash account, T+1 settlement) | yes | ✅ owner decided 2026-10-04: do NOT build (rotations free ~$28/wk ≈ 6% of book; item 2 redeploys them the next Wednesday) |
| 4 | Candidate funnel: skip unbuyable names, backfill from ranking | yes | ✅ owner decided 2026-10-04: do NOT build (Oct 2: 0 of 9 new candidates hard-unbuyable; sector tilt now measured by item 5) |
| 5 | Gate 1a read on the FULL universe (`factor_history.jsonl`), per-period cross-sectional IC, non-overlapping periods, + sector-neutral diagnostic → `quant_ic_report.json` (GH plane) | no (measurement) | ✅ built + tested (PR pending merge) |
| 6 | Counterfactual: date-demeaned returns + Benjamini-Hochberg `p_value_bh` (the field Gate 1b names) | no (measurement) | ✅ built + tested (PR pending merge) |
| 7 | Performance report from daily 4 PM closes (git history of `portfolio_snapshot.json`), annualized at the real sampling frequency; paired-return beta | no (measurement) | ✅ built + tested (PR pending merge) |
| 8 | Log LLM tokens + estimated cost per run, and cost as % of AUM | no (measurement) | ✅ built + tested (PR pending merge) |

## Findings this plan does not act on (logged so they are not lost)

- Composite is a sector sort (Financials 13 of top 30 on 2026-10-02). Not fixed in the
  formula (freeze + §5 kill rule); measured instead by item 5's sector-neutral IC.
- `enforce_net_edge` passes BUYs with `expected_return ≤ 0` or missing; floor is $0.
- News-discovery candidate branches in `analysis._select_candidates` are unreachable.
- Agents sample at temperature 1.0.
- `risk_watch` interlock includes BUY and never-placed tickers (MANUAL_TODO #14).
- Pre-existing: on a CRO API failure, `apply_pm_backstop` still adds its 3-signal exit
  SELLs, so the envelope is non-empty and the routine stamps it — consuming the week's
  rebalance on an outage. (Item 1 injects the stop in that case, since the envelope is
  stamped anyway.) Owner decision whether the backstop should also stand down.
- `/code-review ultra` on this batch (item 1 is execution-path) — owner-launched.
- Live daily routine lacks the post-gate FINAL-ATTEMPT FALLBACK block (pasted before
  commit `824cf37`) — re-paste before Wed.
