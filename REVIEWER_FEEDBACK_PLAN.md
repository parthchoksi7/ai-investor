# Reviewer Feedback — Action Plan (Critical Triage)

Companion to `REVIEWER_FEEDBACK_BACKLOG.md`. Reviewed through three lenses:
backend/capital-integrity, platform/reliability, and ML/AI-skeptic. Triaged
against the **actual objective**, not against "what would satisfy Reviewer #2."

## The governing principle

`PAPER_DRAFT.md` is a **system description that makes no performance claim** and
explicitly enumerates its ablations as future work. That framing is already
honest. Therefore most reviewer "missing experiments" are **not blockers to
publish** — they are correctly disclosed as open. Treating them as requirements
would be letting a hostile reviewer redefine the paper into one it never claimed
to be.

The gate for "do it" is one of:
1. **The product/paper currently misleads** (a live measurement or benchmark that
   flatters the system). The paper criticizes flattering benchmarks and
   denominator-free anecdotes — shipping with them live is self-contradictory.
2. **Real capital, compliance, or position-integrity gap** — improves the product
   regardless of the paper.
3. **Cheap empirical content the paper already admits it needs** and can produce
   from data on hand.

Everything else: disclose as future work (already done) or defer with reason.

---

## Corrections to the backlog (verified against code)

- **A8 is stale / mostly done.** `enforce_net_edge` is wired live —
  `main.py:240` calls it; `guardrails.py:459` implements it against
  `cost_model.net_edge`. The gate is **already blocking live BUYs**. Remaining
  work is not "wire it" but "**validate it isn't silently starving the book**"
  (a live gate dropping trades with no measured effect is its own risk). Reclass
  from PARTIAL-build to **validate** (Tier 2).
- **B14 / B16 are feasible but underpowered.** `agent_log.json` is a list of
  **10 runs**. Ticker-level rates (DA reject %, PM HOLD %) have a usable
  denominator; run-level rates (CRO veto %, uptime) have n≈10. Do them — they
  start the clock and kill the anecdote problem — but report n honestly and
  expect them to firm up over time, not to be conclusive on day one.
- **The backtest already does fills right** (`backtest/engine.py`: signal at
  close(t), fill at open(t+1)). The calibration ledger does **not** (A1). They
  should match; the ledger is the one that's wrong.

---

## TIER 1 — Do now (honesty bugs in the live product + capital/compliance gaps)

These are not "nice for the paper." Each is a defect in the running system.

### T1.1 — Fix calibration entry-price look-ahead (backlog A1)
- **Why:** the ledger is the paper's most transferable artifact, and it currently
  measures returns from the signal-day **close** — a price that is not executable
  (fills are next morning). This inflates apparent skill. The backtest already
  uses open(t+1); make the ledger match.
- **Change:** `calibration.py` — stamp entry as next-session open (re-derive at
  scoring time from the following bar). Mark already-collected rows `legacy` so
  biased and corrected samples are never pooled.
- **Effort:** S. **Lens:** ML (kills a false-positive skill signal at the source).

### T1.2 — Overlapping-window statistics (backlog A2)
- **Why:** daily forecasts on a 21-day horizon share 20/21 of their window. The
  current `1.96/√n` CI overstates precision ~4.6×. The paper already says the
  ledger is the *least* trustworthy component; this makes the math match the
  disclosure.
- **Change:** `calibration.py:agent_scorecard` — report **effective N** and use
  non-overlapping 21-day block sampling (more honest than HAC for a dashboard;
  it visibly collapses N).
- **Effort:** S. **Lens:** ML / quant.

### T1.3 — Multiplicity control + one pre-registered primary metric (backlog A3 + C2)
- **Why:** one IC + one hit-rate per (agent, field) across 5 series = data
  dredging if you report the best. Cheap to fix and it pairs with an external
  pre-registration that costs nothing but credibility-upside.
- **Change:** (a) `calibration.py` — Benjamini-Hochberg adjustment on the
  secondary metrics; (b) commit one primary metric + horizon + benchmark +
  threshold in writing; (c) register it on OSF/AsPredicted (C2) **before** any
  number is reported.
- **Effort:** S (code) + process. **Lens:** ML / quant integrity.

### T1.4 — Wash-sale pre-sale 30-day window (backlog A6)
- **Why:** real IRS §1091 compliance gap on a live taxable account. Today only
  the post-sale re-entry block exists; a loss sale within 30 days *after* a
  purchase is not caught. `tax_lots.py` already reconstructs purchase dates.
- **Change:** extend the wash-sale guardrail to consult `tax_lots.open_lots`
  acquired dates and **block/flag** loss exits within 30 days of a purchase.
- **Effort:** M. **Lens:** backend/capital + compliance. (Decision needed: hard
  block vs. flag-and-allow — see Open Questions.)

### T1.5 — Automated position reconciliation for the crash-recovery state (backlog A7)
- **Why:** the single largest hole in the "autonomous" claim **and** a genuine
  capital risk: `execution_started_at` with no `executed_at` means position state
  is *unknown* until a human looks. Platform severity **P1** (unknown position
  state → can't safely trade next session).
- **Change:** recovery step (in `preflight_gate.py` or a dedicated script) that
  pulls live broker positions via Robinhood MCP, diffs against
  `pending_decisions.json`, and emits a **specific** alert with the diff. Auto-
  clear the flag only when the broker state matches the intended post-trade state;
  otherwise hard-stop with the diff in the alert.
- **Effort:** M. **Lens:** platform (DR/RTO) + backend (reconciliation).

### T1.6 — SPY total-return + exposure/beta benchmark (backlog A4)
- **Why:** the live dashboard compares a dividend-**inclusive** portfolio against
  a price-return SPY, and a partly-cash book against a fully-invested index. Both
  flatter the portfolio. The paper explicitly calls this out as a bias running
  the wrong way — leaving it live contradicts the paper.
- **Change:** `performance.py` + `publish.py` (+ Supabase schema/dashboard) —
  store `spy_total_return` (adjusted close) and report average net exposure +
  realized beta; benchmark on a beta/exposure-matched basis.
- **Effort:** M. **Lens:** all three (it's a correctness bug in reporting).

### T1.7 — Deliberation descriptive statistics (backlog B14)
- **Why:** Section 5 is currently denominator-free anecdote — the exact sin the
  paper criticizes elsewhere. This converts it to measured behavior with **zero
  new data**: CRO veto rate, DA reject rate, PM HOLD rate, DA-flag↔PM-no-buy
  coincidence, inter-agent disagreement, token/cost per agent.
- **Change:** analysis pass over `agent_log.json`; fold a table into §5.
- **Caveat:** n≈10 runs — report counts and n, don't over-claim.
- **Effort:** S. **Lens:** ML (first real empirical content; turns the paper from
  workshop-anecdote to workshop-result).

### T1.8 — Operational base rates + turnover (backlog B16)
- **Why:** same anti-anecdote fix on the ops side; also **de-assumes §6.6** —
  the tax-hurdle paragraph currently uses a worst-case all-short-term assumption
  because turnover is unreported. It's computable.
- **Change:** pass over `system_health.json` history, git history of
  `market_snapshot.json`, `trades.csv`, `tax_lots.py` → run/trade/abort/veto
  counts + realized turnover + ST/LT holding-period split.
- **Effort:** S. **Lens:** platform + ML.

---

## TIER 2 — Do soon (real value, more effort or lower urgency)

### T2.1 — Validate the net-edge gate (backlog A8, reclassified)
- The gate is **live**. The risk now is the opposite of the backlog's: it may be
  silently dropping good BUYs. Backtest its effect through `backtest/` (gate on
  vs. off) and add a logged counter of net-edge rejections to the descriptive
  stats (T1.8). **Effort:** S–M.

### T2.2 — Reproducibility package (backlog A12)
- Log the **resolved** model snapshot string + sampling params (temp, top-p,
  max_tokens) per agent call; export prompt templates as a versioned artifact.
  Cheap logging, large credibility return; the data path is already reproducible,
  this closes the model path. **Effort:** S–M. **Lens:** ML.

### T2.3 — Execution-vs-mark slippage capture (backlog A5)
- Do the **logging** now (capture actual fill price vs. same-day close in
  `execute.py`) so data accrues; defer the report. At current size the dollar
  impact is immaterial, but the data clock should start. **Effort:** S for
  logging. **Lens:** backend.

### T2.4 — Migrate execution trigger off GitHub Actions (backlog D1)
- Real reliability gap (no scheduling SLA; stale-snapshot aborts have already
  happened). **But severity is P2, not P0:** the system fails toward *missed
  trades*, so a skipped job forfeits alpha, it doesn't lose capital. Move the
  data-fetch + execution triggers to a dedicated scheduler with retries/failover;
  pairs naturally with T1.5. Medium priority. **Effort:** M. **Lens:** platform.

### T2.5 — Scholarship pass (backlog F1)
- Verify and add the multi-agent-trading / LLM-debate citations
  (TradingAgents / FinMem / FinRobot line; Du et al., Liang et al.) to §2 with a
  sentence each on how this system differs. Documentation-only; do it before
  submission. **Effort:** S. **Lens:** ML/positioning.

---

## TIER 3 — Build the data clock now, results later (ablations)

The ML lens insists these are the only things that would *validate* the
architecture. The honest position: **none are required to publish the
system-description paper**, but the highest-value ones should have their harness
built now so evidence accrues, exactly like the calibration ledger.

### T3.1 — Single-agent + quant-only shadow pipeline (backlog B1, B2) — highest ablation value
- Build a parallel variant that runs (a) one Sonnet agent with identical context
  and (b) quant-only top-N, on the **same frozen inputs** each day, logging
  decisions without trading. This starts accruing the comparison the paper says
  is the "minimal missing experiment." Results come with elapsed time; the
  *harness* is buildable now. **Effort:** M–L. **Lens:** ML (the decisive
  architecture test).

### T3.2 — Retrospective DA output-quality judge (backlog B11)
- Cheapest genuine test of "is the Devil's Advocate actually critical or just
  adversarial-sounding." LLM-judge over already-logged DA outputs vs. a
  single-agent "consider risks" control on the same inputs. No new market data.
  **Effort:** M. **Lens:** ML.

### T3.3 — Write factor-attribution + factor-benchmark code now, gate on returns (backlog B12, B13, C6)
- The decisive alpha test (regress excess returns on FF + UMD + BAB; compare to
  MTUM/USMV/QUAL/RSP). It **needs a return series** (blocked on time) but the
  factor data is **free** (Ken French, AQR) and the analysis code can be written
  and unit-tested now against the existing curve so it runs the day there's
  enough history. Write `factor_attribution.py` + the free-data loader (C6);
  don't claim results yet. **Effort:** M. **Lens:** ML/finance.

### T3.4 — Multi-seed significance harness (backlog B17)
- Prerequisite for B1/B3/B4/B5/B15 to be *interpretable* (LLM outputs are
  stochastic). **Don't build speculatively** — build it only when committing to
  run an ablation, and reuse it across all of them. Flag as the dependency it is.
  **Effort:** M.

---

## WON'T DO NOW — with reasoning (the ignore bucket)

These are genuinely blocked, over-engineered for current scale, or correctly
already disclosed. Not doing them is the right call.

- **B9 (≥252 trading days), B8 (regime robustness), B10 (100× account):** blocked
  on **real elapsed time**. Cannot be manufactured. Already disclosed as future
  work. *Per the user's instruction, noted and ignored.*
- **B3, B5, B15 (CRO / model / bull-bear ablations):** need cost + B17 harness +
  accumulated sample. Defer until T3.4 exists and there's data. Disclosed.
- **B4 (DA information coefficient):** auto-unblocks once T1.1 fixes the ledger
  and forecasts mature. No separate work — it falls out of the corrected ledger.
- **B6 (factor-weight sensitivity):** would run on the 210-bar survivorship
  snapshot → the result would itself be biased. Blocked on C1/C3. Running it now
  would produce a misleading number; **deliberately not done.**
- **B7 (random-portfolio baseline):** low value; a cheap sim baseline that adds
  little the quant-only baseline (T3.1) doesn't. Skip unless trivially free.
- **C1 (point-in-time universe):** the root of survivorship bias, but needs
  historical index-membership incl. delisted names — paid/hard data. Blocked.
  Honestly disclosed in §3.3. Live forward path is unaffected (can only trade
  what's investable now).
- **C3 (as-filed PIT fundamentals), C4 (real-time earnings calendar),
  C5 (PFOF/NBBO benchmarking):** all blocked on **paid data feeds**. At current
  account size the execution-quality item is immaterial. Disclosed. Revisit if
  the account scales or a feed is budgeted.
- **D2 (intraday monitoring + position-level stop-loss):** **deliberately not
  building the heavy version.** An always-on monitor with halt/liquidation
  authority is a large new failure surface, and a price-triggered stop-loss is an
  *action* that can fire on bad intraday data — directly against the system's
  "fail toward missed trades" philosophy. At current scale, blast radius is
  already bounded by the 10%/12% position and 25% sector caps, no leverage, no
  shorts. Correct move: **keep as a disclosed limitation** (§6.10 already does).
  If anything, add a *cheap* EOD drawdown re-check, not an intraday service.
- **E1–E5 (acknowledged-unfixable):** inherent (LLM backtest contamination,
  novelty ceiling, concentration variance, venue positioning, factor-vs-alpha
  prior). Disclosure-only by nature; already in the paper. Do not re-litigate.

---

## Recommended sequence

1. **Honesty-bug sprint (Tier 1, code):** T1.1 → T1.2 → T1.3 → T1.6. These are
   live measurements that currently flatter the system; fix before the paper goes
   out. (~days)
2. **Capital/compliance sprint:** T1.4 (wash-sale) + T1.5 (reconciliation).
   Improves the product irrespective of the paper. (~days)
3. **Empirical-content pass:** T1.7 + T1.8 — turns §5 from anecdote into measured
   behavior; pairs with the pre-registration from T1.3. (~1–2 days)
4. **Pre-submission polish:** T2.2 (reproducibility), T2.5 (citations), T2.1
   (validate the live net-edge gate).
5. **Data clock (parallel, no deadline):** T3.1 shadow pipeline + T3.3 factor
   code, so evidence accrues for a future results paper.
6. **Defer with intent:** T2.4 infra migration; T3.2/T3.4 when committing to
   ablations.

## Open questions (need a human decision)

1. **Wash-sale (T1.4): hard block or flag-and-allow?** A hard block on loss exits
   within 30 days of purchase can trap a position the risk layer wants out of.
   Recommend **flag + allow the SELL but suppress the loss-harvest claim**, since
   capital-risk exit should outrank tax optimization. Confirm.
2. **Pre-registration (T1.3): which single primary metric?** Recommend shrunk
   rank-IC of the **quant composite** at the 21-day horizon vs. the candidate
   universe, as the one pre-committed number. Confirm before registering.
3. **Publish timing:** is the paper going out *before* or *after* the Tier-1
   honesty fixes land? Strong recommendation: **after** — several §s describe the
   corrected behavior, so the code and paper should ship together.
