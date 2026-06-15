# Reviewer Feedback Backlog — Items Not Yet Implementable

This file tracks every piece of reviewer feedback on `PAPER_DRAFT.md` that **could
not be addressed by editing the paper alone**, because it requires a code change,
new data, a multi-week/-month experiment, real elapsed time, or an external
process. Each item records its source, what it addresses, *why* it is blocked
today, and what it would take to close.

Documentation-only fixes (clarifications, corrected claims, added caveats) have
already been applied to `PAPER_DRAFT.md` and are **not** listed here. This file
is the "do later" queue.

Review passes captured:
- **R2** = hostile conference/journal "Reviewer #2" pass (novelty, multi-agent
  skepticism, missing experiments, production reality).
- **Q** = buy-side quant credibility pass (data leakage, look-ahead,
  survivorship, overfitting, benchmark integrity, statistics).
- **ML** = ML-systems reviewer pass (NeurIPS/ICML/ICLR lens: contribution,
  ablations, agent-evaluation, reproducibility).
- **FP** = finance-professor pass (asset pricing: factor exposure vs. alpha,
  benchmark appropriateness, economic rationale, regime dependence).

Status legend: `OPEN` (not started) · `PARTIAL` (some scaffolding exists) ·
`BLOCKED` (waiting on external data/time) · `DONE` (landed; see changelog).

---

## ✅ Implemented — Tier 1 sprint (2026-06-14)

The "Do now" tier from `REVIEWER_FEEDBACK_PLAN.md` (live honesty bugs + capital
gaps) has landed. All changes ship with unit tests; full suite green
(`pytest test_pipeline.py` → all pass). Decisions on the two open questions:
wash-sale = **flag-and-allow** (a wash sale only *defers* the loss, so a
risk/conviction exit must never be blocked to save tax timing); pre-registered
primary metric = **quant.composite_score IC at the 21-day horizon** (largest,
most stable, deterministic sample — the foundation the LLM layer sits on).

| Item | What landed | Files |
|------|-------------|-------|
| **A1** DONE | Calibration entry re-based to the **next-session open** (executable price), derived at scoring time. `signal_close` kept as reference only; one-bar look-ahead removed. No legacy data existed to migrate (ledger files not yet created). | `calibration.py`, `test_pipeline.py` |
| **A2** DONE | Overlapping-window correction: **block-sampled effective N** (non-overlapping ~21-day spacing), `ic_block`/`hit_rate_block`, and CI/p-values based on effective N, not raw count. | `calibration.py` |
| **A3** DONE | **Benjamini-Hochberg** multiplicity control across all (agent,field) metrics + a pre-registered `PRIMARY_METRIC` (quant.composite_score, 21d) flagged in the scorecard `_meta`. External OSF/AsPredicted registration (C2) still pending — that's a process step. | `calibration.py` |
| **A6** DONE | Wash-sale **pre-sale** side (§1091): `flag_wash_sale_presale` flags (never blocks) a loss SELL within 30d of a purchase, using `tax_lots` acquired dates. Annotation rides on the SELL into `pending_decisions.json` + a DEGRADED health record. | `guardrails.py`, `main.py` |
| **A7** DONE | Automated crash-state reconciliation: new `reconcile.py` diffs **live broker positions** vs intended orders, classifies none/all/partial-filled, writes `reconciliation_report.json`, and emits a specific alert. Wired into `preflight_gate.py` (report-only, fail-safe). Safe auto-remediation gated behind `python reconcile.py --apply` (only the two unambiguous cases). | `reconcile.py`, `preflight_gate.py` |
| **A4** DONE (code) | SPY benchmark on **total-return** basis (dividend gross-up, ~1.25%/yr) — removes the flattering bias; plus **average net exposure** and **realized beta**. Local report fully fixed; dashboard SPY cumulative converted to total-return (no schema change). **Still pending:** Supabase columns for `net_exposure`/`realized_beta` (a DB migration not runnable from here — see A4 below). | `performance.py`, `publish.py` |
| **B14** DONE | Deliberation base rates from `agent_log.json`: CRO veto rate, DA reject rate, PM trade rate, DA-flag↔PM-no-buy, bull/bear conflict, regime mix. | `deliberation_stats.py` |
| **B16** DONE | Operational base rates + **realized turnover** and ST/LT holding-period split (de-assumes §6.6's worst case). | `deliberation_stats.py` |

**Empirical findings produced by B14/B16 (n=10 runs, 2026-06-08→06-12 — base
rates with wide error, not conclusions):**

- **CRO full-veto rate = 30% (3/10 runs).** Quantifies the paper's "fired on
  several occasions." The CRO is an active gate, not decorative.
- **Devil's Advocate `recommend_reject` = 0 across 132 evaluations.** In this
  window the adversarial agent's veto signal is **empirically vacuous** — it never
  recommended a reject. This is material for the paper's untested
  "adversarial-value" claim and for B4/B11/B15: there is currently *no* DA reject
  signal to measure an IC on. Worth surfacing in §5/§6.2 as a measured fact, and
  it raises a design question — is the DA threshold/prompt mis-calibrated, or is
  the bear case genuinely never decisive? (Pairs with B15 bull/bear model parity:
  the DA runs on the weaker model.)
- **PM trade rate = 21.5% of candidates** (78.5% no-trade); 1.9 executed
  trades/run; **70% of runs are no-trade runs**. Consistent with the HOLD-default
  design; turnover lower than "daily deliberation" implies.
- **Position-review REDUCE/EXIT rate = 49%.**
- **One-way period turnover ≈ 0.63**; realized ST/LT split **not yet computable**
  — all 9 sells are "uncovered" (positions opened before transaction logging), so
  §6.6's tax assumption can't be replaced with measured numbers *yet* (needs more
  round-trips with in-log cost basis).

**Generated artifacts (git-ignored data, regenerate via the scripts):**
`agent_scorecards.json` (calibration), `reconciliation_report.json` (reconcile),
`deliberation_stats.json` (B14/B16), `performance_report.json` (A4),
`reproducibility.json` + `prompts/` (A12), `health_history.jsonl` (B16).

### Second sprint follow-ups (2026-06-15)

- **A12** DONE — per-call resolved-model + token-usage capture
  (`analysis._record_call`) and `analysis.export_reproducibility` →
  `reproducibility.json` + hashed prompt export, wired into `main.py`. Closes the
  token/cost gap too. Sampling recorded (not pinned).
- **B16 health-history** DONE — `health.save()` now appends a compact line to
  `health_history.jsonl` (append-only), so abort/uptime base rates become
  computable over time. (Preflight-level SKIP_RETRY skips still leave no row —
  they are "no run", not "aborted run".)
- **A7 alerting** DONE — `reconcile._record_health` writes a `crash_reconciliation`
  health check (FAILED on MANUAL_REQUIRED) so `alert.yml` can open an issue;
  recovery runbook added at `DEPLOYMENT.md` §9.3 (`reconcile.py` / `--apply`).
- **Hardening** — the wash-sale pre-sale flag call in `main.py` is now wrapped so
  an annotation failure can never break the trade path (it is observational).
- C2 external pre-registration DONE — AsPredicted #296637
  (https://aspredicted.org/zm7a2p.pdf), wired into the paper + `calibration._meta`.

---

## A. Code changes (system can do; not yet built)

### A1. Calibration ledger: base forward returns on the next executable price
- **Source:** Q §1.1 (confirmed in `calibration.py:74,142`).
- **Addresses:** one-bar look-ahead. `entry_price` is the signal-day **close**
  (`prices[ticker]["close"]`), and `realized_return = (future_px - entry)/entry`.
  The signal is computed from that same close and fills happen the next morning,
  so the return is measured from a price one bar ahead of the first tradable
  price. Inflates apparent skill.
- **Why not done now:** requires changing `log_forecasts()` to stamp the
  next-session open (or a near-9:45 VWAP) as the entry, which means the ledger
  must either wait one bar before stamping entry or re-derive entry at scoring
  time from the following bar.
- **What it takes:** edit `calibration.py` (`log_forecasts`, `score_matured`);
  re-base any already-collected rows or mark them legacy. Note: the **backtest**
  (`backtest/engine.py`) already does this correctly (close(t) → open(t+1)); the
  ledger should match it.
- **Status:** ✅ DONE (2026-06-14). `score_matured` now derives the executable
  next-session open at scoring time; `signal_close` retained as reference only.
  No legacy rows existed. See changelog.

### A2. Calibration ledger: overlapping-window statistics
- **Source:** Q §3.1 (confirmed `DEFAULT_HORIZON=21`, daily logging,
  `ci_halfwidth = 1.96/sqrt(n)` at `calibration.py:202`).
- **Addresses:** daily forecasts over a 21-day horizon share 20/21 of their
  return window → autocorrelation → effective sample ≈ n/21. The current CI
  treats observations as independent and overstates significance ~√21 ≈ 4.6×.
- **Why not done now:** needs a Newey-West/HAC estimator or non-overlapping
  21-day block sampling in `agent_scorecard()`, plus an "effective N" report.
- **What it takes:** edit `calibration.py:agent_scorecard`; decide overlap
  correction vs block sampling (block sampling collapses N and is the more
  honest display).
- **Status:** ✅ DONE (2026-06-14). Block sampling (non-overlapping ~horizon
  spacing) chosen; `n_effective`, `ic_block`, and effective-N CIs reported. See
  changelog.

### A3. Calibration ledger: multiplicity control + single pre-registered primary metric
- **Source:** Q §3.3 / §3.4; R2 (data dredging risk).
- **Addresses:** one IC + one sign-hit-rate per `(agent, field)` across 5+
  series, plus a tunable 21-day horizon. Reporting the best is data dredging.
- **Why not done now:** requires (a) committing one primary metric+horizon
  *before* reporting, and (b) a Benjamini-Hochberg/Bonferroni adjustment on the
  rest.
- **What it takes:** small code addition to scorecard output; a written
  pre-registration (see C2).
- **Status:** ✅ DONE in code (2026-06-14): BH adjustment + `PRIMARY_METRIC`
  (quant.composite_score, 21d) in the scorecard. ⚠ External pre-registration
  (C2, OSF/AsPredicted) is the remaining process step — register BEFORE
  publishing any number.

### A4. Live benchmark: SPY total return + exposure/beta matching
- **Source:** Q §4.1, §4.2 (confirmed: `performance.py:60` portfolio curve =
  account `total_value` incl. dividend cash ≈ total-return; `performance.py:20`
  SPY = price-return; `publish.py` same).
- **Addresses:** the dashboard compares a dividend-inclusive portfolio against a
  price-return SPY, which **flatters** the portfolio; and a partially-cash book
  vs fully-invested SPY is not risk-matched.
- **Why not done now:** requires pulling SPY **adjusted close** (total return)
  and computing/reporting average net exposure + realized beta, then
  benchmarking on a beta-matched basis.
- **What it takes:** edit `performance.py` and `publish.py` (and the Supabase
  schema / dashboard) to store `spy_total_return` and exposure/beta fields.
- **Status:** ✅ DONE in code (2026-06-14). `performance.py` reports SPY total
  return + avg net exposure + realized beta; alpha vs total return is the
  headline. `publish.py` converts the dashboard SPY cumulative to total-return
  (no schema change needed). ⚠ **Remaining (DB migration, not runnable from the
  repo):** add `net_exposure` and `realized_beta` columns to the
  `portfolio_snapshots` table to surface those two on the dashboard. SPY total
  return uses a documented dividend gross-up (~1.25%/yr); an exact adjusted-close
  series (yfinance/Polygon) is a later refinement.

### A5. Execution-vs-mark slippage line item
- **Source:** Q §4.3.
- **Addresses:** NAV is marked at close; fills occur ~9:45 in the wide-spread
  opening window; PFOF execution quality is unmeasured. The gap is a systematic
  basis that can flatter the equity curve.
- **Why not done now:** needs fill-price capture vs same-day close/VWAP and a
  realized-slippage report; the live trade log must record actual fill prices.
- **What it takes:** extend `execute.py` logging + a slippage report in
  `performance.py`.
- **Status:** OPEN.

### A6. Wash-sale: track the pre-sale 30-day window
- **Source:** R2 (§6 evidence attack on guardrails); reflected in `PAPER_DRAFT`
  §3.5.
- **Addresses:** IRS §1091 applies 30 days **before and after** a loss sale.
  `guardrails.py` enforces the post-sale re-entry block only.
- **Why not done now:** requires lot-level tracking of purchase dates against
  prospective loss exits (partial data exists in `tax_lots.py`).
- **What it takes:** extend the wash-sale guardrail to consult `tax_lots.py`
  purchase dates and block/flag loss exits within 30 days of a purchase.
- **Status:** ✅ DONE (2026-06-14). `flag_wash_sale_presale` in `guardrails.py`
  **flags (does not block)** loss exits within 30d of a purchase, wired into
  `main.py`. Decision: flag-and-allow — a wash sale defers the loss, so a
  capital-risk exit must outrank tax timing. See changelog.

### A7. Automated position reconciliation for the crash-recovery state
- **Source:** R2 (production reality); Q (operational); `PAPER_DRAFT` §3.6, §6.8.
- **Addresses:** `execution_started_at` with no `executed_at` currently requires
  **manual** inspection of broker positions — a gap in the "autonomous" claim.
- **Why not done now:** needs an automated reconciliation that pulls actual
  broker positions (Robinhood MCP) and diffs against `pending_decisions.json`,
  plus a targeted alert.
- **What it takes:** new reconciliation step in `preflight_gate.py` / a recovery
  script; alert wiring in the health workflow.
- **Status:** ✅ DONE (2026-06-14). New `reconcile.py` (pure diff +
  live-broker-fetch orchestrator) wired into `preflight_gate.py` (report-only,
  fail-safe — still SKIP_DONE). Safe auto-remediation behind
  `python reconcile.py --apply` (only the unambiguous none/all-filled cases).
  ⚠ Follow-up: wire the non-zero `reconcile.py` exit into the alert workflow so a
  MANUAL_REQUIRED result pages a human (D1-adjacent).

### A8. Net-edge gate (tax + cost hurdle) validation
- **Source:** R2 + Q §6.6; `PAPER_DRAFT` §6.6.
- **Addresses:** trades should clear an after-cost, after-tax hurdle before
  execution. `cost_model.py` exists; the gate is designed but not validated/live.
- **Why not done now:** requires wiring the cost/tax model into the decision path
  and validating it doesn't degrade the pipeline.
- **What it takes:** integrate `cost_model.py` into `guardrails.py`/`main.py`;
  backtest the gate's effect.
- **Status:** ⚠ CORRECTION (2026-06-14): this item was **stale**. The gate is
  **already live** — `enforce_net_edge` (`guardrails.py:459`) is called in
  `main.py:240` and blocks BUYs whose after-cost, after-CA-ST-tax edge is below
  the floor. The remaining work is the *opposite* risk to the one described:
  **validate it isn't silently starving the book** (backtest gate on/off; log a
  net-edge-rejection counter into the B16 stats). Reclassified PARTIAL→validate
  (Tier 2 in the plan).

### A9. Route Devil's Advocate signal to the CRO (or test the alternative)
- **Source:** R2 (hostile pass, architectural decoupling); confirmed
  `analysis.py:583` produces the DA output, while the CRO receives only the PM
  trade list + portfolio — not `recommend_reject` or the DA bear case.
- **Addresses:** the only agent with veto authority (CRO) never sees the
  adversarial signal. `recommend_reject` reaches the PM as advice but not the
  layer that can actually block a trade, so the adversarial design has no teeth
  at the enforcement stage.
- **Why not done now:** requires a design decision (does feeding DA flags to the
  CRO reintroduce the anchoring the isolation was meant to prevent?) plus an A/B
  to measure veto quality with vs without the DA flag.
- **What it takes:** add DA flags to the CRO prompt behind a config switch;
  compare veto decisions and downstream outcomes. Pairs with B3 (CRO ablation).
- **Status:** OPEN.

### A10. Candidate-selection breadth (reserve slots for low-score names)
- **Source:** R2 (hostile pass, opportunity-set funnel); confirmed
  `analysis.py:394` `select_candidates` = holdings + top composite-score names
  (`MAX_CANDIDATES=20`, momentum 0.30 / low-vol 0.25 weighted) + incidental news
  tickers.
- **Addresses:** the LLM agents deliberate almost only over names the quant layer
  already favors, so the "variant perception / market is wrong" mandate cannot
  surface a low-momentum, deep-value thesis — such names enter the candidate set
  only incidentally via news, never by reservation.
- **Why not done now:** requires reserving a fraction of candidate slots for
  low/contrarian-score names (or a separate value/mean-reversion screen) and
  testing it does not just add noise.
- **What it takes:** edit `select_candidates` in `analysis.py`; evaluate via the
  calibration ledger or a contrarian-slot A/B.
- **Status:** OPEN.

### A11. Make `thesis_correct` reflect `invalidates_if` materialization
- **Source:** R2 (hostile pass, paper-vs-code mismatch); confirmed
  `journal.py:135` sets `thesis_correct` from realized-return sign / half of
  `expected_return`, **not** from whether the stated `invalidates_if` condition
  occurred.
- **Addresses:** the paper's falsifiability claim (Contribution #4, §3.7) was
  partially aspirational — there is no programmatic check that the stated
  invalidation condition materialized; the flag is a realized-return proxy. The
  paper text is now corrected to disclose this; the *fix* is to implement the
  stronger evaluation.
- **Why not done now:** verifying materialization requires structured/checkable
  `invalidates_if` predicates (price/fundamental/event triggers), not free text.
- **What it takes:** constrain `invalidates_if` to checkable predicates; add a
  materialization checker invoked at exit in `journal.py`.
- **Status:** OPEN.

### A12. Reproducibility package: pin model snapshots, publish prompts + decoding params
- **Source:** ML (reproducibility) / editor (reproducibility); reflected in new
  `PAPER_DRAFT` §6.11.
- **Addresses:** the released code lets a reader inspect the pipeline, but exact
  replay of a historical decision is not possible without (a) pinned model
  snapshot IDs (not just `claude-sonnet-4-6` / `claude-haiku-4-5` family names),
  (b) the verbatim agent system+user prompts, and (c) the decoding/sampling
  parameters (temperature, top-p, max tokens). The data path is already
  reproducible (committed snapshots); the model path is not.
- **Why not done now:** requires exporting the prompt templates and sampling
  config into a versioned artifact and recording the exact model snapshot string
  returned by the API per run, alongside the existing `market_snapshot.json`.
- **What it takes:** log the resolved model ID + sampling params per agent call;
  publish prompt templates; document the reproducibility package in the repo.
- **Status:** ✅ DONE (2026-06-15). `analysis._record_call` captures the resolved
  `response.model` snapshot + token usage per call; `analysis.export_reproducibility`
  writes `reproducibility.json` (resolved snapshots, usage, sampling params, SDK
  version) + verbatim prompt files with SHA-256 hashes, called per run from
  `main.py`. This also closes the B14 per-agent **token/cost** gap (usage is now
  captured). ⚠ Sampling params are **recorded as API defaults, not pinned** —
  pinning would change live behavior and is deferred as a deliberate change.

---

## B. Experiments / ablations (need elapsed time or model runs)

> All of these are already listed as "Missing Experiments" in `PAPER_DRAFT` §6.3.
> They are tracked here because none can be completed today — they need either
> accumulated live data, a paper-shadow run, or a dedicated A/B harness.

### B1. Single-agent baseline (7-agent vs 1-agent) — **highest priority**
- **Source:** R2 §2, §7; Q.
- **Blocked by:** needs a parallel pipeline variant + enough decision days to
  compare decision quality / realized returns. No A/B harness exists yet.
- **Status:** OPEN.

### B2. Quant-only baseline (LLM adds value over quant scores?)
- **Source:** R2 §7.
- **Note:** `backtest/strategies.py` (`quant_momentum_vol`, `equal_weight_topn`)
  gives a quant-only backtest path; a **live** A/B against the LLM book does not
  exist.
- **Status:** PARTIAL.

### B3. CRO ablation (were vetoed names worse than approved?)
- **Source:** R2 §2; Q §6.1. Blocked by sample size + an ablation run.
- **Status:** OPEN.

### B4. Devil's Advocate IC (`recommend_reject` vs forward returns)
- **Source:** R2 §6; Q §6.1. Blocked by matured forecast sample in the ledger.
- **Status:** OPEN.

### B5. Model ablation (all-Sonnet vs all-Haiku vs mixed)
- **Source:** R2 §2. Blocked by cost + a comparison harness.
- **Status:** OPEN.

### B6. Factor-weight sensitivity (0.30/0.25/0.20/0.25 robustness)
- **Source:** R2 §4; Q §5.
- **Note:** runnable through `backtest/` once a longer/PIT history exists; on the
  current 210-bar survivorship snapshot the result would itself be biased.
- **Status:** BLOCKED on B8/C1.

### B7. Random-portfolio and market-beta baseline
- **Source:** R2 §7. Blocked by the live comparison window.
- **Status:** OPEN.

### B8. Market-regime robustness (bear market / momentum crash)
- **Source:** R2 §3, §7; Q §3.2. Blocked by **real elapsed time** — the system
  has only run in one regime; no way to fabricate other regimes without true PIT
  history.
- **Status:** BLOCKED on time / historical data.

### B9. ≥252 trading days live track record before any edge claim
- **Source:** R2 §3; Q. Blocked by elapsed time (internal threshold is 60 days;
  strong inference wants 252+).
- **Status:** BLOCKED on time.

### B10. 100× paper-shadow account results
- **Source:** R2 §3, §5; `PAPER_DRAFT` §6.1. Account is being built; produces
  capital-scale evidence, **not** new independent trade cycles.
- **Status:** BLOCKED on time.

### B11. Agent output-quality evaluation (domain expert or LLM-judge)
- **Source:** R2 §6; ML (§ ablation: "is the Devil's Advocate genuinely
  critical, or superficially adversarial?"). A retrospective LLM-judge ablation
  on already-logged DA outputs vs a single-agent "consider risks" control is the
  cheapest version and needs no new market data.
- **Blocked by:** an eval harness / rater; the single-agent control must be run
  on the same inputs.
- **Status:** OPEN.

### B12. Factor attribution regression — **decisive alpha test**
- **Source:** FP §1, §6.9 of `PAPER_DRAFT`; ML (causality vs correlation).
- **Addresses:** whether any realized return is alpha or just factor exposure.
  Regress portfolio excess returns on Fama-French factors + momentum (UMD) +
  betting-against-beta (BAB); report the intercept and its t-stat.
- **Blocked by:** needs a live return series of meaningful length (B9) and a
  factor-returns source (Ken French data library for FF/UMD; AQR for BAB).
- **What it takes:** a `factor_attribution.py` analysis once returns exist.
- **Status:** BLOCKED on time (return history) + factor data (free for FF/UMD).

### B13. Factor-benchmark comparison (MTUM / USMV / QUAL / RSP)
- **Source:** FP §2; `PAPER_DRAFT` §4.4, §6.3 item 11.
- **Addresses:** SPY alone is the wrong benchmark for a factor tilt; the system
  must be shown to beat the cheap ETF that replicates its own core.
- **Blocked by:** a live return series; ETF price histories are free.
- **Status:** BLOCKED on time (return history).

### B14. Deliberation descriptive statistics — **feasible now, no market data**
- **Source:** ML (the most fixable gap: zero behavioral characterization of the
  pipeline); `PAPER_DRAFT` §6.3 item 12, Conclusion.
- **Addresses:** converts qualitative architecture claims into measured behavior:
  CRO veto rate, DA `recommend_reject` rate, PM HOLD rate, DA-flag↔PM-no-buy
  coincidence, inter-agent disagreement, token/cost per agent.
- **Blocked by:** nothing external — `agent_log.json` (≈550KB) already holds the
  data. This is an analysis pass, not new data collection.
- **What it takes:** an `analysis.py`/notebook pass over `agent_log.json`; fold
  the resulting table into `PAPER_DRAFT` §5 as the first real empirical content.
- **Status:** ✅ DONE (2026-06-14). `deliberation_stats.py:deliberation_stats`.
  Key findings in the changelog: **CRO veto 30%**, **DA reject 0/132**, PM trade
  rate 21.5%. Token/cost-per-agent NOT covered (not logged — lands with A12).
  Next: fold the table into `PAPER_DRAFT` §5.

### B15. Bull/bear model-parity ablation (DA on Sonnet vs Haiku)
- **Source:** R2 (hostile pass, model-asymmetry); confirmed `analysis.py:50-51`
  (Devil's Advocate runs `MODEL_FAST`=Haiku; Portfolio Manager runs
  `MODEL_SMART`=Sonnet).
- **Addresses:** the bull/idea side (PM) runs on the stronger model while the
  adversarial bear side (DA) runs on the weaker one — a systematic capability
  bias toward the bull case. B5 tests the aggregate mix but not bull/bear parity
  specifically.
- **Blocked by:** an A/B harness running the DA on Sonnet on identical inputs and
  comparing `recommend_reject` IC / bear-case quality.
- **What it takes:** extends B5; swap the DA model behind a config flag and
  compare. Cheapest version reuses already-logged DA inputs (no new market data).
- **Status:** OPEN.

### B16. Operational base rates with denominators — **feasible now**
- **Source:** R2 (hostile pass): Section 5 reports "several occasions," "some
  names," etc. without denominators — the same denominator-free anecdote the
  paper criticizes elsewhere.
- **Addresses:** quantifies run count, trade count, abort/skip count, CRO-veto
  rate, DA-reject rate, and uptime over the deployment window, converting Section
  5 anecdotes into base rates.
- **Blocked by:** nothing external — `system_health.json` history, git commit
  history of `market_snapshot.json`, `trades.csv`, and `agent_log.json` already
  hold the data. Overlaps B14 (behavioral stats); this item is the
  operational-reliability slice (runs/aborts/uptime).
- **What it takes:** an analysis pass over the health/log/git history; fold a
  counts table into `PAPER_DRAFT` §5.
- **Status:** ✅ DONE (2026-06-14). `deliberation_stats.py:operational_stats`:
  run/trade counts, no-trade-run rate (70%), kill-switch activations, one-way
  turnover (~0.63). ⚠ **Abort/skip/uptime NOT covered** — `system_health.json`
  is overwritten each run, so aborted runs leave no row; needs health-history
  retention (a small follow-up: append each run's health to a JSONL).
- **Extension (turnover / holding-period distribution):** ✅ turnover computed;
  ⚠ ST/LT holding-period split **not yet meaningful** — all current sells are
  "uncovered" (positions opened before transaction logging), so §6.6's worst-case
  assumption stands until more round-trips accrue with in-log cost basis.

---

### B17. Multi-seed / significance harness for all LLM-comparison ablations
- **Source:** ML (scientific rigor); reflected in `PAPER_DRAFT` §6.3 intro, §6.11.
- **Addresses:** the LLM agents are sampled stochastically, so any single-run
  A/B (B1 single-agent, B3 CRO, B4 DA, B5 model, B15 bull/bear parity, B11 output
  quality) can confound sampling noise with a real effect. Each arm must be run
  over multiple seeds on shared frozen inputs and compared as a distribution with
  a significance test, not a point estimate.
- **Why not done now:** no multi-seed harness exists; cross-cuts every B-item
  that compares LLM configurations and multiplies their cost.
- **What it takes:** a harness that fixes inputs, runs N seeds per arm, and
  reports effect size + significance. Prerequisite for B1/B3/B4/B5/B11/B15 to be
  interpretable.
- **Status:** OPEN.

## C. Data / external dependencies

### C1. Point-in-time universe (kill survivorship bias)
- **Source:** Q §2.1 (confirmed `market_data.py:402`,
  `all_tickers = WATCHLIST | SP500_HOLDINGS.keys()` — static current membership).
- **Addresses:** every historical IC/backtest over this universe is upward-biased.
- **Blocked by:** needs historical index-membership data (point-in-time S&P 500
  constituents incl. delisted names).
- **What it takes:** a PIT membership source + universe reconstruction in
  `market_data.py`.
- **Status:** BLOCKED on data.

### C2. External pre-registration (OSF / AsPredicted)
- **Source:** R2 §3; Q §3.3, §3.4; `PAPER_DRAFT` §4.4.
- **Addresses:** the 60-day threshold and primary metric are currently internal
  and changeable without detection.
- **Blocked by:** an external registration action (process, not code) — register
  primary metric, horizon, benchmark, and threshold before reporting.
- **Status:** ✅ DONE (2026-06-15). Registered: **AsPredicted #296637**,
  https://aspredicted.org/zm7a2p.pdf (public, immutable). Primary metric,
  21-day horizon, executable next-open basis, SPY total-return benchmark, BH
  control, and 60-day threshold all locked. Wired into `PAPER_DRAFT` §4.4 + §3.7,
  `calibration.PREREGISTRATION_URL`/`_meta`, and `PREREGISTRATION.md`.

### C3. Point-in-time / as-filed fundamentals (restatement-free)
- **Source:** Q §1.2, §6.5 (EDGAR company-facts returns currently-on-file,
  restatement-contaminated values).
- **Addresses:** look-ahead in the quality/valuation factors and their historical
  IC; enables the full 4-factor backtest (B6, A-side).
- **Blocked by:** a paid as-filed PIT fundamental feed (or careful as-filed EDGAR
  reconstruction).
- **Status:** BLOCKED on data/subscription.

### C4. Real-time confirmed earnings calendar
- **Source:** R2 §6; `PAPER_DRAFT` §6.5. Earnings dates are currently estimated.
- **Blocked by:** a keyed real-time fundamentals/earnings feed.
- **Status:** BLOCKED on data/subscription.

### C5. PFOF / execution-quality benchmarking vs NBBO
- **Source:** Q §4.3; `PAPER_DRAFT` §3.6.
- **Blocked by:** needs NBBO reference data to quantify price-improvement
  shortfall on Robinhood fills.
- **Status:** BLOCKED on data.

### C6. Factor-return + factor-ETF data (supports B12, B13)
- **Source:** FP §1, §2.
- **Addresses:** inputs for factor attribution and factor-benchmark comparison.
- **Blocked by:** acquisition only — FF/UMD factors are free (Ken French data
  library), BAB is free (AQR), factor-ETF histories are free. No subscription
  needed; this is a wiring task gated by B9/B12 existing.
- **Status:** OPEN (data freely available; not yet wired in).

---

## D. Infrastructure

### D1. Migrate off GitHub Actions to dedicated scheduling (SLA + failover)
- **Source:** R2 §5; `PAPER_DRAFT` §3.1, §6.8.
- **Addresses:** GitHub Actions has no scheduling SLA; stale-snapshot aborts have
  already occurred; it is not purpose-built trading infrastructure.
- **What it takes:** move the data-fetch and execution triggers to a dedicated
  cloud scheduler with retries/failover.
- **Status:** OPEN.

---

### D2. Intraday risk monitoring + position-level stop-loss
- **Source:** CRO/risk pass (no real-time risk control); reflected in new
  `PAPER_DRAFT` §6.10.
- **Addresses:** the pipeline runs once daily (9:45 ET + retries); the 20%
  drawdown kill switch and 50% sell-notional circuit breaker are evaluated only
  at that time. Between runs there is no monitoring and no position-level
  stop-loss, so an intraday flash crash / single-name gap / fast drawdown cannot
  trigger any response until the next cycle. Single-name and sector exposure are
  bounded by the 10%/12% and 25% caps, but a correlated intraday move is not.
- **Why not done now:** requires an always-on monitoring process with intraday
  price polling and halt/liquidation authority — separate infrastructure from the
  once-daily routine.
- **What it takes:** a monitoring service (intraday quotes + drawdown/stop checks)
  with authority to halt or liquidate, plus alerting; pairs with D1 (dedicated
  scheduling/infra).
- **Status:** OPEN.

---

## F. Scholarship / positioning

### F1. Add comparisons to multi-agent LLM trading + debate literature
- **Source:** editor/ML (missing citations; novelty positioning). Related Work
  (`PAPER_DRAFT` §2) cites FinAgent [6] and FinGPT [5] but omits the most directly
  comparable recent line — *multi-agent* LLM trading systems and LLM debate for
  reasoning quality — which bears on the novelty framing of the seven-agent
  design.
- **Addresses:** the paper's positioning would be stronger (and more honest about
  prior art) by engaging works such as multi-agent LLM trading frameworks and
  memory-augmented trading agents (e.g., the TradingAgents / FinMem / FinRobot
  line) and multi-agent *debate* for reasoning/factuality (e.g., Du et al.,
  "Improving Factuality and Reasoning in Language Models through Multiagent
  Debate"; Liang et al. on multi-agent debate). Several of these instantiate
  debate/critic roles similar to the Devil's Advocate and may weaken or
  contextualize the architectural-novelty framing.
- **Why not done now (documentation-only, but flagged not inserted):** exact
  bibliographic details (authors, year, venue, arXiv IDs) must be verified before
  insertion — per the "don't make anything up" constraint, candidate references
  are listed here for verification rather than written into the paper with
  possibly-wrong metadata.
- **What it takes:** verify each citation, add to §2 with a sentence each on how
  the present system differs (live capital + deterministic safety layer +
  idempotency), and re-check that no novelty claim is undercut.
- **Status:** OPEN.

---

## E. Acknowledged-but-unfixable (state-only; no action expected)

These were raised by reviewers but are **inherent** and can only be disclosed,
not fixed. They are recorded so they are not re-litigated as open work.

- **E1. LLM backtest contamination.** Model training cutoffs postdate any
  backtestable period; the LLM layer cannot be backtested out-of-sample. Even
  live large-cap decisions are partly contaminated by pretraining knowledge of
  those companies. (R2 §4 / Q §1.3) — *disclosed in `PAPER_DRAFT` §6.4.*
- **E2. Novelty ceiling.** Idempotency, prompt caching, and the RAG-style memory
  loop are engineering practice, not methodological novelty. The contribution is
  integration + live deployment. (R2 §1) — *reframed in `PAPER_DRAFT` §1, §2.*
- **E3. Concentration-driven variance.** An 8–15 name book is dominated by
  idiosyncratic risk, widening every confidence interval; only more time/breadth
  helps, and breadth is limited by cross-sectional correlation. (Q §3.2 /
  R2 §3) — *disclosed in `PAPER_DRAFT` §6.1.*
- **E4. Not a main-track ML contribution.** Without ablations, the paper is a
  systems/evaluation case study, not a methodological ML advance; its venue is
  applied/systems or a workshop. This is a positioning fact, not a defect to fix
  by editing — only the experiments in §B change it. (ML) — *stated in
  `PAPER_DRAFT` §1 "Scope and venue positioning".*
- **E5. Factor exposure is the default explanation, not alpha.** The quant core
  is a momentum/low-vol/quality/value bundle; absent a significant
  factor-adjusted intercept (B12), the prior is no edge. There is no a priori
  economic reason an LLM reading public data finds persistent mispricing
  (semi-strong EMH). This is a standing interpretive stance, disclosed, not an
  open task. (FP §1, §6, §7) — *disclosed in `PAPER_DRAFT` §3.3, §6.9.*

---

*Last updated: 2026-06-14 (Tier 1 implementation sprint: A1/A2/A3/A4/A6/A7/B14/B16
landed — see ✅ changelog at top; A8 corrected as already-live; new files
calibration.py·guardrails.py·reconcile.py·performance.py·publish.py·deliberation_stats.py,
all with tests, full suite green. Earlier same day, 4-persona review: added A12, B17, D2, F1; extended
B16 with turnover/holding-period; paper edits for regime-injection accuracy, CRO
input precision, §6.6 turnover assumption, §6.10 intraday cadence, §6.11
reproducibility). Earlier: R2 hostile re-review added A9–A11, B15–B16.
Maintainer note: when an item lands, move its
substance from "future work / caveat" phrasing in `PAPER_DRAFT.md` to a stated
result, and delete the item here.*
