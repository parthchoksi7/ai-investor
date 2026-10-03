# Plan — Oct 3 2026 remediation (the performance investigation's fix list)

**Status:** 🔄 IN PROGRESS. Owner-directed 2026-10-03: "fix everything today."
**Branch:** `fix/oct3-remediation`, one PR. **Deploy = merge to `main`**, only after owner OK.
**Must be live before:** Wed 2026-10-07 rebalance. Every routine-prompt change is
backward-compatible in both directions, so merge order versus routine sync cannot break a run.

Legend: ⬜ not started · 🔄 in progress · ✅ done · ⏸ deferred (reason given)

---

## 0. Why this batch exists (evidence, re-verified 2026-10-03)

Full investigation: see the conversation summary in memory, plus the artifacts cited below.
Independent sources: Robinhood MCP (orders, buying power, realized P&L) and Polygon sector ETFs.

- **Return:** bot −0.80% vs SPY +3.35% (Jun 8 → Oct 1). The gap splits about equally into
  three parts: avoiding tech, picks lagging their own sector, and cash. The gap is ~0.6σ of
  this book's measured 12.7%/yr tracking error. **Not evidence of skill or its absence.**
- **The real problem:** the experiment is not producing a clean answer.
  - A whole week (W39) was missed: GitHub billing blocked every job Sep 21–30.
  - Nothing alerted, because alerting runs on the same blocked plane.
  - The −25% stop-loss check skipped 5 trading days.
  - The Oct 2 rotation half-executed: proceeds in a cash account are unspendable until settled.
  - The routine trades on different scores than the experiment records (62 short-history names).
  - Ticker `PARA` is now Banzai International (a $2.4M micro-cap), not Paramount.
  - The book's market exposure (~0.55) sits below the 0.8–1.2 the scale rule requires.

## 1. Governance constraints (read before changing anything)

- **Measurement window.** `SCALE_DECISION_RULE.md` §6 freezes `FORMULA_VERSION`, prompts,
  universe and sizing. Phases 2–3 change prompts, universe and policy, so they **restart the
  window**.
  - The window opened 2026-09-02 and is already contaminated: the missed W39 and the 8-day
    blackout must be logged regardless.
  - Owner decision (2026-10-03): bundle every window-resetting change here, restart **once**,
    then freeze.
  - The new window opens at the first rebalance after merge + routine sync (target Wed 2026-10-07).
- **`FORMULA_VERSION` does NOT change.** No factor weight or sub-score formula moves. Fix 2.1
  makes the routine *consume* the existing formula's full-history output.
  - Gate 1a's quant evidence clock (`factor_history.jsonl`) is therefore **preserved**.
  - Only Gate 1b (the AI) and Gate 2 (the account) restart.
- **Policy change-control (IPS §18.4).** Every new or changed limit goes into `policy.yaml`
  AND `policy.py._DEFAULTS` in the same commit, with validators (`TestPolicyParity`).
  `policy_version` → `2.1-oct3-remediation`.
- **Invariants.** Every guard is subtractive; it never invents an order. Every failure resolves
  toward missed trades, never duplicates. No fix may weaken the claim/stamp envelope, the
  operate-on-`main` rule, or GUARD 4 (risk_watch never BUYs).
- **Deploy gates (DEPLOYMENT §7.0).**
  - `RELEASE_NOTES.md [Unreleased]` entry.
  - `/code-review high` (run here).
  - `/code-review ultra` for the execution-path items (1.2, 1.3). Only the owner can launch
    ultra.
- **No `DRY_RUN main.py`.** It is the weekend, so the snapshot is stale and preflight would
  abort. `pending_decisions.json` must never be committed from a dev run.
  Validation = pytest (incl. the `run_daily_cycle` smoke suite) + targeted replays.

---

## Phase 1 — Safety & execution correctness (no evidence-clock impact)

| # | Fix | Finding it closes | Files | Routine sync? | Status |
|---|-----|-------------------|-------|---------------|--------|
| 1.1 | **Stop-loss runs even when the rebalance can't.** On a rebalance-eligible day (Wed, or Thu/Fri catch-up) whose rebalance is blocked by stale snapshot/dossier/API, the gate's **final attempt** (≥12:30 ET) routes to RISK-WATCH (exit 30) instead of SKIP-RETRY. A risk_watch envelope does not count as a rebalance attempt, so the Thu/Fri catch-up stays available. | `preflight_gate.py:270-311` skipped `risk_watch` for 5 trading days (Sep 23–25, Sep 30–Oct 1) — contrary to P1-7 | `preflight_gate.py`, tests | No (exit 30 already handled) | ⬜ |
| 1.2 | **Size BUYs to settled cash.** New subtractive guard `enforce_settled_cash`, run last: total BUY notional ≤ settled buying power × (1 − buffer); over-budget BUYs scaled down proportionally with qty recomputed, and dropped under the $5 floor. Routine STEP 1 records `buying_power` from `get_portfolio`; absent → falls back to pre-trade `cash` (today's behavior, never looser). | Oct 2: SELLs filled, all 3 funded BUYs never reached the broker ($32.65 spendable vs $134 needed) | `guardrails.py`, `main.py`, `execute.py`, `policy.yaml`/`policy.py` (`settled_cash_buffer_pct`), `ROUTINE_DAILY_CYCLE.md` STEP 1, tests | **Yes** (STEP 1 field; optional — absent = old behavior) | ⬜ |
| 1.3 | **Order IDs from code, broker errors recorded, real fill prices.** `main.py` and `risk_watch.py` stamp a `ref_id` (uuid4) on every BUY/SELL in the envelope; the routine passes it through and never hand-types one. Unplaced orders' error text goes to `order_errors.json` and into the `reconciliation` check. After each order the routine reads `average_price` back from `get_equity_orders`. | LLM-typed ref_ids (`a1b2c3d4-0001-…`, `b2c3d4e5-0010-…`) risk a duplicate-key collision; Oct 2 left no error text; `trades.csv` stores decision quotes, not fills (realized −$10.94 estimate vs −$6.70 broker) | `main.py`, `risk_watch.py`, `journal.py`, `ROUTINE_DAILY_CYCLE.md` STEP 4/5, tests | **Yes** (falls back to generated uuid if the envelope lacks one) | ⬜ |
| 1.4 | **Alerts that mean something.** `health.py` gains `actionable_status`: worst status, excluding *informational* checks whose status is DEGRADED (`decision_validation`, `cash_discipline`, `wash_sale_presale`). `alert.yml` opens/closes issues on `actionable_status` (falls back to `overall_status`). `overall_status` is unchanged for every other consumer. | Every weekly rebalance since July opened a DEGRADED issue; the Oct 2 execution failure looked like routine noise | `health.py`, `.github/workflows/alert.yml`, tests | No | ⬜ |
| 1.5 | **Test isolation.** The suite must not write the real `health_history.jsonl`. | 666 fixture rows (`run-abc`, …) in the local file | `test_pipeline.py` | No | ⬜ |

## Phase 2 — Data & measurement integrity (restarts the window)

| # | Fix | Finding it closes | Files | Status |
|---|-----|-------------------|-------|--------|
| 2.1 | **Trade on the scores the experiment records.** `fetch_snapshot.py` embeds its full-history `score_all_tickers` output (`precomputed_scores`: as_of, formula_version, scores) into the snapshot *before* slimming. `main.py` uses it when `as_of == data_date` and the formula matches; otherwise it scores locally and records `quant_scores` DEGRADED. | 62 tail names scored on a 63-day beta in the routine vs a 252-day one in `factor_history.jsonl` (MTB 74.5 traded vs 71.4 recorded) | `fetch_snapshot.py`, `main.py`, tests | ⬜ |
| 2.2 | **Reused-ticker guard + PARA.** Remove `PARA` (now Banzai International); add `PSKY` (Paramount Skydance) after verifying it. New generic guard: a non-held name whose 210-day fetch returns fresh but **young** history (< 126 bars) is excluded from scoring and listed in `data_quality` — new listings and reused symbols look identical, and both are unfit to rank. | Polygon reference 2026-10-03: PARA = Banzai, $2.4M cap, 39 bars | `universe.py`, `market_data.py`, `data_quality.py`, tests | ⬜ |
| 2.3 | **Deterministic universe (MANUAL_TODO #24).** Expansion-gate hysteresis (enter ≥ 80% coverage, exit only < 70%) and a coverage denominator that doesn't flap with the rotating batch. | Oct 2 rebalance saw 175 names; the next snapshot 164 — the candidate set depends on cron timing | `market_data.py`, `universe.py`, tests | ⬜ |
| 2.4 | **Data on demand.** A rebalance-day attempt that finds a stale snapshot gets a distinct gate exit (**11**, SKIP-RETRY-STALE). The routine then pushes `data_request.json` (once per day, no `[skip ci]`), which triggers `market_data.yml` immediately. Push-triggered runs exit early if today's snapshot already exists. An unsynced routine treats 11 like 10 (stops). | Scheduled crons start 10 AM–3:30 PM ET; rebalances ride the last retry | `preflight_gate.py`, `.github/workflows/market_data.yml`, `ROUTINE_DAILY_CYCLE.md` STEP 0, tests | ⬜ |
| 2.5 | **Reconcile the audit logs to the broker.** Pull every agentic-account order via MCP into a committed evidence file. `reconcile_fills.py --apply` writes real fill prices into `trades.csv` / `transactions.json` and backfills BUY lots missing from the log (the 12 "uncovered" sells). Acceptance: computed realized P&L ≈ broker's −$6.70. | `trades.csv` price ≠ fill; 12 uncovered sells; after-tax figure UNRECONCILED | new `reconcile_fills.py`, data files, tests | ⬜ |

## Phase 3 — Decision layer (restarts the window; owner-directed)

| # | Fix | Finding it closes | Files | Status |
|---|-----|-------------------|-------|--------|
| 3.1 | **Tell the PM and CRO what they're measured against.** A code-computed block: benchmark = SPY; current and projected book beta (`beta_stable`, cash = 0); the policy band 0.8–1.2; SPY sector weights vs the book, sourced and dated in `policy.yaml`, omitted if no trustworthy source. Information, not a mandate. The CRO's objective becomes survival **and** a fair test vs SPY; exposure is context, **never a veto trigger** (the Aug 19 lesson). | `_PM_SYSTEM`/`_CRO_SYSTEM` never name the benchmark; "negative beta" cited as a BUY reason; book beta 0.2 (life) / 0.55 (now) vs Gate 2's 0.8–1.2 | `analysis.py`, `policy.yaml`/`policy.py` (`target_beta_min/max`, benchmark weights), tests | ⬜ |
| 3.2 | **PM sees settled buying power** — plans rotations it can actually execute (pairs with 1.2). | Oct 2 | `analysis.py` | ⬜ |
| 3.3 | **Selling needs a reason, not an expired timer.** Gain-aware min-hold: a discretionary SELL of a position **at a gain** needs ≥ 63 trading days (losers keep 30 — harvesting a loss is tax-favorable). PM tags and the guard share one helper so they cannot disagree. PM prompt: a discretionary SELL must name which recorded invalidation condition has occurred; "REDUCE + low alpha" alone is not one. Risk exits exempt as always. | 5 of 8 weekly-regime sells landed 42–49 days after purchase — the min-hold became the holding period; 11/11 realized lots short-term | `guardrails.py`, `analysis.py`, `policy.yaml`/`policy.py` (`min_holding_trading_days_gain`), IPS §7.2, tests | ⬜ |
| 3.4 | **Wash-sale window is inclusive.** Re-entry blocked through day 30 (IRS §1091: "within 30 days after"). | `guardrails.py:698` `days < 30`; EOG sold Jun 22 at a loss, rebought Jul 22 (day 30) = wash sale | `guardrails.py`, `policy.yaml` comment, IPS §7.3, tests | ⬜ |

## Phase 4 — Governance & documents

| # | Fix | Status |
|---|-----|--------|
| 4.1 | `SCALE_DECISION_RULE.md` §7: log the contamination (W39 missed, Sep 21–30 blackout, half-executed Oct 2) and the single restart, with the freeze commitment and the new window-open date. | ⬜ |
| 4.2 | `IPS.md`: §8 line 297 claims a "tech/mega-cap-tilted" book (the opposite is true); §7.2 / §7.3 / Appendix A–B for 1.2, 3.1, 3.3, 3.4. | ⬜ |
| 4.3 | Skill files (`portfolio_manager`, `ml_ai_engineer`, `chief_risk_officer`, `tax_strategist`): replace stale hard-coded figures with dated current values + "read the artifact, not this number". | ⬜ |
| 4.4 | `RELEASE_NOTES.md`, `CLAUDE.md` (changelog, Investment Rules, routine notes, Known Limitations), `MANUAL_TODO.md` (close #24; add owner actions). | ⬜ |

## Phase 5 — Make the data job ~40× cheaper (flag-gated; ships last)

| # | Fix | Status |
|---|-----|--------|
| 5.1 | Polygon grouped-daily (`/v2/aggs/grouped/...`, every US ticker in one call) for the **incremental daily bar**, plus the splits reference endpoint to re-fetch any ticker with a new split. Behind `POLYGON_FETCH_MODE=grouped` (default **off**). Ships only with a parity test proving identical bars to the per-ticker path on recent days. Turning it on is a separate owner decision: it changes the data substrate, though not the formula. | ⬜ |

## Phase 6 — Verify & ship

- ⬜ `pytest test_pipeline.py -q` green; ruff `F821,F823`; workflow YAML parses
- ⬜ `/code-review high` — every finding resolved
- ⬜ PR opened; owner launches `/code-review ultra` (execution path)
- ⬜ Owner OK → merge to `main`
- ⬜ Live routine sync of `ROUTINE_DAILY_CYCLE.md` (STEP 0/1/4/5) before Mon 2026-10-05 9:45 ET
  (backward-compatible either way)

## Owner actions (cannot be done from code)

1. ✅ GitHub billing / spending limit — fixed 2026-10-03.
2. ⬜ GitHub → Settings → Notifications → Actions: email on failed workflows. This is the one
   alert path that still works when Actions itself is blocked.
3. ⬜ Launch `/code-review ultra` on the PR.
4. ⬜ Approve merge; approve (or perform) the live-routine sync.

## Verified non-issues (investigated, deliberately not changed)

- `_select_candidates` news branches are **not dead code**: they fill slots when fewer than
  20 names are scored (degraded runs). Unchanged.
- **Removing the low-volatility factor — withdrawn.** On 2026-10-02 data it moves tech from
  3 to 4 of the top 12. Since formula 3.0 (Aug 27) tech candidates appear every week. Not
  worth a `FORMULA_VERSION` reset.
