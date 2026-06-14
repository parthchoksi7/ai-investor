# Edge-Upgrade Solution Plans — Expert Panel

Planning doc for six improvements (#1, #2, #3, #5, #6, #9) from the critical
review. Each is planned by **two external expert archetypes** with a deliberate
point of tension, then resolved into a concrete plan grounded in the actual
codebase (`analysis.py`, `quant_engine.py`, `main.py`, `journal.py`,
`guardrails.py`).

> Status: PLAN ONLY. No code written. Sequencing and the shared spine are at the
> bottom — read that first if you only read one section.

---

## The shared spine (build these once; five of six plans depend on them)

1. **Prediction ledger — `forecasts.jsonl`** (append-only). Every agent field is
   already a structured forecast; we just never score it. #2 writes it, #9 adds
   to it, #5 consumes the calibrated weights, #3 reads it to *forward-test* the
   LLM layer. This is the single highest-leverage artifact.
2. **Cost/tax model — `cost_model.py`.** One implementation shared by the live
   gate (#6) and the backtest (#3) so live and simulated economics match.
3. **Point-in-time archive.** The daily-committed `market_snapshot.json` git
   history *is* a PIT record already. #1 enriches each snapshot (fundamentals +
   earnings calendar); #3 reads the history.
4. **The training-cutoff truth.** A frozen LLM "knows" the future of any
   historical period (its weights saw 2024 when backtesting 2023). Therefore:
   **backtest the deterministic layers (#3/#5/#6); *forward-test* the LLM layers
   (#2/#9).** This is stated loudly in every relevant plan and is non-negotiable
   for intellectual honesty.

---

## #1 — LLM as reasoner over verified data (not oracle of stale facts)

**The problem in the live system:** prompts say *"use your training knowledge of
{ticker}'s earnings calendar"* and the model dutifully invents specifics —
the real Jun 12 trade rationales contain *"Q2 earnings July 16," "Q3 July 28,"*
which are model-generated, unverified, and stale-by-cutoff, yet drive real
orders. Separately, all `fundamentals` are `None` on free Polygon, so the quant
quality/valuation factors are permanently N/A — the "4-factor" composite is
really momentum + inverse-vol.

### Expert A — Lena Petrov, Market-Data Platform Lead (ex-Bloomberg / quant-fund data eng)
- Introduce a provider abstraction; pick one vendor that covers **earnings
  calendar + consensus estimates (and revisions) + fundamentals** in one key.
  Realistic candidates: Financial Modeling Prep, Finnhub, Tiingo, Polygon paid.
- Ship the cheapest feed that covers calendar+estimates; reliability and a clean
  ingestion contract matter more than breadth.

### Expert B — Dr. Rajiv Menon, Fundamental-Data Quant (point-in-time / Compustat lineage)
- The gotcha is **point-in-time correctness**. As-reported fundamentals get
  *restated*; a vendor's "current" value didn't exist on the trade date. For
  **live** trading you only ever see "now," so an as-reported feed is fine. For
  the **backtest (#3)** an as-reported feed is poison — instant look-ahead.
- Earnings-date accuracy specifically: confirmed vs estimated dates differ; the
  fabrication risk we're fixing is exactly here.

### Resolution
- **Live path:** cheap as-reported feed is acceptable.
- **Backtest path:** you cannot afford true PIT data, so **build your own** by
  snapshotting the feed daily into the `market_snapshot.json` git history (the
  pattern already exists). The git history becomes your PIT archive going
  forward; accept that pre-inception backtests of fundamentals are
  approximate and say so.

### Concrete plan
- **New `data_providers.py`** — interfaces `FundamentalsProvider`,
  `EarningsCalendarProvider`, `EstimatesProvider`; one concrete impl (e.g.
  `FMPProvider`). Keep Polygon for OHLCV.
- **`market_data.py`** — extend the snapshot dict with `earnings_calendar`
  ({ticker: next_date, confirmed?}), `estimates` ({ticker: {eps, rev, revision_trend}}),
  and real `fundamentals` ({ticker: {gross_margin, op_margin, fcf_margin, de, pe,
  fcf_yield, ev_ebitda}}). Respect the 5-call/min free-tier discipline already in
  the file (or the paid tier removes it).
- **`quant_engine.py`** — *no code change needed*; `compute_quality_score` /
  `compute_valuation_score` already consume exactly these fields and the honest
  composite auto-upgrades from 2-factor to 4-factor the moment real fundamentals
  arrive. This is the cleanest win in the whole plan.
- **`analysis.py`** —
  - `run_earnings_catalyst_analyst`: inject the verified `next_earnings_date`
    and estimate-revision trend; add a **fabrication guard** — after parsing, if
    the model's `next_earnings_est` disagrees with the calendar, overwrite with
    the real date and set a `fact_corrected` flag (logged).
  - `run_research_analyst`: inject a `VERIFIED FACTS` block; instruct the model
    to ground catalysts in it and mark any catalyst not backed by data as
    `speculative`.
- **DoD:** quant composite shows real quality/valuation; no agent emits an
  earnings date that contradicts the calendar; `fact_corrected` count tracked in
  `system_health.json`.
- **Effort:** M (1 vendor integration + prompt/threading changes). **Risk:** low
  (additive). **Unlocks:** real factors, kills the top fabrication vector,
  enriches the PIT archive #3 needs.

---

## #2 — Calibration / learning loop (the dead feedback loop, resurrected)

**The problem:** all 7 journal entries show `thesis_correct=None`,
`actual_return=None`. Every agent emits a forecast; nothing is ever scored. The
system cannot tell a thesis that worked from one that blew up.

### Expert A — Dr. Hannah Wu, Forecasting & Calibration Researcher (Brier / reliability diagrams)
- Treat every agent field as a probabilistic forecast with a horizon. Score with
  **Brier score / log-loss** for the binary flags (`recommend_reject`,
  `thesis_correct`) and **Information Coefficient (rank correlation to forward
  return)** for the numeric scores (`confidence`, `composite_score`,
  `earnings_alpha_score`, `hold_score`, `cro_risk_budget`).
- Recalibrate with isotonic/Platt so "confidence 7/10" maps to an empirical hit
  rate. Publish reliability diagrams.

### Expert B — Sofia Almeida, Quant Signal-Combination PM (multi-analyst blending)
- **17 trades is far too few** — naive per-agent hit rates will be pure noise and
  you'll overfit. Shrink every estimate toward a "no-skill" prior; widen CIs;
  don't let calibration move sizing until n is large.

### Resolution (the key insight that makes this work at $500 scale)
- **Score the full candidate universe, not the trade set.** Every agent already
  scores ~20 candidates/day regardless of whether they're traded. That is
  ~20 forecasts/agent/day → hundreds/month — enough to estimate skill with
  shrinkage long before the trade count would. Outcomes are realized forward
  returns from the price history (counterfactual/paper outcomes for un-traded
  names), so you don't need executions to learn.
- Gate calibration's influence on the PM behind a sample threshold
  (e.g. n ≥ 200 matured forecasts/agent) with Bayesian shrinkage to prior until
  then.

### Concrete plan
- **New `calibration.py`:**
  - `log_forecasts(run_id, date, pipeline_state, candidates, prices)` → append
    one line per (agent, ticker, field, value, horizon) to `forecasts.jsonl`.
    Source data is already in `pipeline_state` — this is wiring, not modeling.
  - `score_matured(snapshot_history)` → for each forecast whose horizon has
    elapsed, join to realized fwd return (1w/1m/3m) from the snapshot archive;
    write `forecasts_scored.jsonl`.
  - `agent_scorecard()` → per-agent, per-field IC / Brier / hit-rate with
    Ledoit-style shrinkage and CIs → `agent_scorecards.json`.
- **`main.py`** — after `record_run`, call `calibration.log_forecasts(...)`. Add a
  separate scheduled job (or piggyback on the EOD routine) that runs
  `score_matured` + `agent_scorecard`.
- **`analysis.py`** — `run_portfolio_manager`: inject a **calibration block**
  ("Devil's Advocate rejects: 68% correct over n=240; Research confidence IC=0.04
  — weak") from `agent_scorecards.json`. This turns multi-agent theater into a
  measured ensemble.
- **`journal.py`** — already has `close_position` populating `thesis_correct`;
  keep it, but the universe-level scoring in `calibration.py` is what produces
  statistical power.
- **DoD:** reliability diagram per agent; PM prompt carries real hit rates;
  calibration weights gated behind n-threshold; `forecasts_scored.jsonl` grows
  daily.
- **Effort:** M-L. **Risk:** low (observational until weights are switched on).
  **Unlocks:** the labels #5 and #9 both need; the only path to *knowing* whether
  any agent has skill.

---

## #3 — Backtest + walk-forward harness

**The problem:** strategy changes ship on "pytest green," never "backtest
improved." There is no way to know if the strategy beats buy-and-hold.

### Expert A — Dr. Marco Reyes, Systematic Backtest Engineer (look-ahead / survivorship)
- Two-tier design. **Tier 1: quant-only fast backtest** (no LLM) — replay
  `score_all_tickers` + a sizing rule over historical bars; deterministic, runs
  in CI. **Tier 2: full-pipeline replay** — expensive, periodic.
- Walk-forward windows so any tuned threshold (the hand-set `PE<15→90` ladders)
  is *fit out-of-sample*, not hardcoded.

### Expert B — Tom Becker, Execution / TCA specialist
- Fills must be realistic: model spread + slippage from vol + a fill assumption
  (next-open, not same-close). Costs and capacity matter even commission-free.
- **The LLM-in-the-loop killer:** replaying agents over years is costly AND the
  model's training cutoff means it already knows the outcome. A 2023 backtest
  with a 2026-cutoff model is contaminated look-ahead that no caching fixes.

### Resolution (the honest conclusion)
- **Backtest the deterministic layers fully** (quant signal, sizing #5, costs/tax
  #6) over multiple years — these are clean.
- **Do NOT pretend to backtest the LLM layer.** Validate it **forward** via the
  prediction ledger (#2) and paper-shadow at scale. State the cutoff hazard
  prominently. (This mirrors the repo's existing honesty about fabricated
  earnings and the rejected earnings-gate.)

### Concrete plan
- **New package `backtest/`:**
  - `engine.py` — event loop over a historical bar archive; next-open fills;
    daily rebalance to target weights.
  - `costs.py` — *imports the shared `cost_model.py`* so backtest economics ==
    live economics.
  - `strategies.py` — pluggable: `quant_momentum`, `quant_vol_sized` (#5);
    each reuses `quant_engine.score_all_tickers` **unchanged** so the backtest
    scores exactly what production scores.
  - `report.py` — CAGR, ann. vol, Sharpe, Sortino, max drawdown, turnover,
    hit-rate, **gross vs net-of-tax**, vs SPY. (Supersedes today's `performance.py`,
    which is live-only and SPY price-return.)
- **Data:** loader that walks the committed `market_snapshot.json` git history
  (PIT, going forward) + a one-time bulk OHLCV download for pre-inception years.
- **Walk-forward:** rolling train/validate; quant thresholds become fit
  parameters with OOS reporting.
- **CI gate:** a fast smoke backtest; block merges that regress the quant-only
  Sharpe beyond tolerance.
- **DoD:** `python -m backtest --strategy quant_vol_sized --from 2022-01-01`
  prints a full report; CI runs the smoke test; a documented "we forward-test the
  LLM, we don't backtest it" note.
- **Effort:** L. **Risk:** medium (data alignment / look-ahead discipline).
  **Unlocks:** the only credible validation for #5 and #6; this is the
  foundation — build it first.

---

## #5 — Risk-based position sizing (math decides *how much*, LLM decides *what*)

**The problem:** the LLM picks `target_weight`; high-beta and low-beta names get
the same 8%. No vol targeting, no conviction scaling, no covariance.

### Expert A — Dr. Yusuf Karim, Portfolio Construction Quant (vol targeting, Ledoit-Wolf)
- Inverse-vol base weights (risk-parity-ish), then scale the whole book to a
  **target portfolio volatility** using a shrunk covariance matrix. Risk
  contribution, not dollar weight, is the unit of allocation.

### Expert B — Elena Vasquez, Bet-Sizing Practitioner (fractional Kelly)
- With **8 names and ~120d history the covariance is garbage** — don't optimize
  per-name on it. Use inverse-vol + a conviction tilt, fractional Kelly (¼–½) on
  conviction, hard 10% cap. Keep it robust, not clever.

### Resolution
- Inverse-vol **base**; **conviction tilt** (blend `research.confidence`,
  `devil.overall_risk_score`, and — once #2 matures — the calibrated agent
  weights, plus the #9 disagreement haircut); shrunk covariance used **only** for
  the portfolio-level vol scalar (not per-name optimization); fractional Kelly
  with the existing ≤10% position / ≤25% sector guardrails as hard caps.

### Concrete plan
- **New `sizing.py`:** `size_positions(decisions, conviction_inputs, cov, regime,
  portfolio) -> decisions` (fills `target_weight`).
- **`quant_engine.py`:** add `compute_covariance(history_map, tickers)` — extend
  the existing `compute_return_correlations` with a Ledoit-Wolf-shrunk covariance.
- **`analysis.py`:** change PM output schema to emit `direction` (BUY/SELL) +
  `conviction` (0–1) instead of choosing `target_weight`. The PM stops being a
  sizer.
- **`main.py`:** call `sizing.size_positions(...)` **before** `_compute_qty` and
  `validate_decisions` (so guardrails still clamp the math output).
- **Validation:** backtest (#3) old (LLM-weight) vs new (risk-sized) on the *same*
  signal; ship only if Sharpe ≥ baseline.
- **DoD:** realized portfolio vol tracks target; high-beta names sized smaller
  than low-beta at equal conviction; backtest improvement demonstrated.
- **Effort:** M. **Risk:** medium (interacts with guardrails ordering).
  **Depends on:** #3 (to prove it), benefits from #2 (calibrated conviction).

---

## #6 — Tax- and cost-aware trade gate

**The problem:** "expected value" is computed gross. The account churns weekly,
in a taxable wrapper, so nearly every gain is short-term ordinary income — a 2%
pre-tax momentum swap can be net-negative.

### Expert A — Dana Okafor, Direct-Indexing / Tax-Aware Engineer (lots, wash sales, HIFO)
- Track **tax lots** (cost basis is only `avg_price` today). On SELL, select lots
  (HIFO default), compute realized **short-vs-long-term**, watch the wash-sale
  window. Add a holding-period guard near the 1-year LT boundary.

### Expert B — Tom Becker, TCA specialist (returns from #3)
- At $500, commission-free, fractional: explicit costs ≈ 0. The dominant net-edge
  terms are **tax + behavioral churn drag**, not commissions. So the gate's real
  job is to *throttle the documented churn*, not shave basis points.

### Resolution
- Build lot accounting now (cheap; #3 needs it for honest after-tax backtest),
  but the **live gate is primarily a turnover/holding-period/net-edge throttle**:
  reject trades whose expected net edge (agent `expected_return` − est. cost −
  est. tax on the realized lot) < `MIN_NET_EDGE`; warn/block selling a near-LT
  winner for a marginal reason.

### Concrete plan
- **New `cost_model.py`** (the shared spine): `round_trip_cost(ticker, notional,
  vol)`, `tax_drag(realized_gain, holding_days)` (ST vs LT rate),
  `net_edge(decision, …)`.
- **New `tax_lots.json`** (or extend `journal.py`): per-lot {ticker, qty,
  cost_basis, acquired_date}; SELLs select lots (HIFO) → realized ST/LT.
- **`guardrails.py`:** add `enforce_net_edge` after `enforce_sector_limits` —
  skip trades below `MIN_NET_EDGE` (fold into the existing `decision_validation`
  health check); add the holding-period guard.
- **`journal.close_position`:** record realized ST/LT and after-tax return →
  richer labels for #2.
- **`backtest/costs.py`:** import the same `cost_model.py` → after-cost,
  after-tax backtest reports.
- **DoD:** sub-threshold trades skipped and logged; backtest reports gross AND
  net-of-tax; measured turnover drops.
- **Effort:** M. **Risk:** low-medium. **Shares:** `cost_model.py` with #3.

---

## #9 — Model-disagreement as a conviction signal

**The idea:** run the same prompt across diverse models; disagreement = epistemic
uncertainty = a conviction haircut.

### Expert A — Dr. Priya Nair, Uncertainty Quantification / Ensembles
- Disagreement across a diverse ensemble is a clean epistemic-uncertainty proxy.
  Apply it where uncertainty matters most — the **decision node**, not everywhere.

### Expert B — Aaron Stein, LLM-Eval / Multi-Model Orchestration
- Running 3 models × 20 candidates × 4 agents is 12× tokens for marginal value,
  and string-diff "disagreement" is meaningless. Measure agreement **semantically
  on the structured decision** (same ticker? same side? target within one
  bucket?), and only where it pays.

### Resolution
- **Ensemble only the portfolio-level decision agents** — PM final list, CRO
  veto, and the Devil's `recommend_reject` — with 2–3 models (Fable 5 / Opus /
  Haiku). That's ~3 calls × 2-3 models = a handful of extra calls/run, trivial
  cost. Never ensemble the 20-ticker research fan-out.
- Measure agreement on the **structured decision** (action/side/weight-bucket),
  optionally embedding-similarity for theses.
- **Crucially: validate before trusting.** Log `disagreement_score` to the #2
  ledger and confirm it has negative IC to forward returns *before* it haircuts
  any size. Don't assume disagreement = bad; measure it.

### Concrete plan
- **New `ensemble.py`:** `ensemble_call(models, system, user_msg, schema)` →
  (list of parsed outputs, `disagreement_score`); `disagreement(outputs, schema)`
  — structured agreement metric.
- **`analysis.py`:** wire `ensemble_call` into `run_portfolio_manager`,
  `run_chief_risk_officer`, and the Devil's reject flag. Emit
  `disagreement_score` per decision.
- **Wiring:** `disagreement_score` → logged to `forecasts.jsonl` (#2) and passed
  to `sizing.py` (#5) as a conviction haircut — **haircut weight = 0 until #2
  confirms** the signal predicts worse outcomes.
- **DoD:** `disagreement_score` logged per run; a calibration result on whether
  high-disagreement decisions underperform; haircut activated only if confirmed.
- **Effort:** S-M. **Risk:** low (cheap, gated). **Depends on:** #2 to validate;
  feeds #5.

---

## Sequencing & dependency graph

```
        ┌─────────────────────────────────────────────┐
        │  #3 backtest harness + cost_model.py spine   │  ← FOUNDATION (validate everything)
        └───────────────┬─────────────────────────────┘
                        │
   ┌────────────────────┼───────────────────────────────┐
   │                    │                                │
 #1 real data     #2 prediction ledger            (PIT archive feeds #3)
 (real factors,   + calibration  ◄────────────┐
  kills fab.)      (learning spine, labels)    │
                        │                       │
              ┌─────────┴─────────┐             │
              │                   │             │
        #5 risk sizing      #9 disagreement ────┘  (gated behind #2)
        (consumes #2)        (feeds #5 haircut)
              │
        #6 tax/cost gate (shares cost_model with #3)
```

**Recommended build order:**
1. **#3 quant-only backtest + `cost_model.py`** — nothing else can be validated
   without it; the cost model is shared with #6.
2. **#1 real data feed** — biggest single quality jump (real quality/valuation
   factors *for free* via existing quant code), kills the top fabrication vector,
   enriches the PIT archive #3 consumes.
3. **#2 prediction ledger + calibration** — the learning spine; produces the
   labels #5 and #9 need; beats the small-sample problem by scoring the full
   candidate universe, not the trade set.
4. **#5 risk-based sizing** — validated in #3, consumes #2's calibrated conviction.
5. **#6 tax/cost gate** — shares #3's cost model; throttles the documented churn.
6. **#9 disagreement signal** — cheapest, gated behind #2 validation, haircuts #5.

**The $500 caveat (applies to #2 and #9 especially):** statistical power at
$500 / 17 trades is near zero. The full-universe forecast scoring in #2 buys a
lot of headroom, but to actually *validate* #5/#6/#9 you will want a
**paper-shadow account at ~100× size** running the identical pipeline in
parallel. That was improvement #6 in the original review's wider list (paper
shadow) and is the statistical prerequisite that makes the calibration and
disagreement signals measurable rather than anecdotal. Strongly recommend
standing it up alongside #3.
```
