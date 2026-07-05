# Strategy Redesign Plan — Weekly Cadence, Long-Term Horizon, Measurement Harness

> Working design doc from the Jun 23–24 2026 critical review of the daily cycle.
> Captures: the run audit, the root-cause findings (quant researcher + veteran PM
> lens), verdicts on every proposed fix, and the two concrete designs the owner
> approved — **weekly cadence (fixed Wednesday)** and **switching on the measurement
> harness** — plus the sequencing for the **shift off momentum to a medium/long-term
> strategy**.
>
> **Status (updated 2026-07-05): BUILT AND DEPLOYED.** Phases 0–5 described below —
> including the live-path weekly cadence and `risk_watch.py` (§6, §11–14) — shipped
> 2026-06-27 through 2026-07-04; a post-go-live hardening batch followed 2026-07-05.
> This document is kept as the **design rationale record** (why each decision was
> made, what was rejected and why) — it is NOT the current build-status tracker.
> For live status, always check (newest first): `CLAUDE.md`'s dated Changelog
> sections, `MANUAL_TODO.md`'s roadmap table + go-live observation window, and
> `RELEASE_NOTES.md`. Phase 6 (exit-logic rewrite) is the only phase below still
> unbuilt. All code changes shipped behind the DEPLOYMENT §7.0 gates (RELEASE_NOTES
> update + expert `/code-review` + tests + weekend dry-run where applicable).
>
> **Rev 2 (Jun 26 2026)** — revised after a four-lens critical review (senior
> backend / ML-AI / security / quant-PM). Material changes: (1) **cadence is a
> fixed Wednesday**, not a rotating weekday — the rotation was stateful
> over-engineering for an academic benefit at this scale; (2) **`risk_watch.py`
> is a decision-generator on the *existing* execution envelope**, not a new order
> path; (3) **build order re-sequenced** to front the deterministic, backtest-now
> work; (4) two premises corrected against the code — `log_forecasts` silently
> stopped on Jun 18, and the fundamental-coverage bug lives in the GitHub Actions
> enrichment path, not the cloud routine (SEC EDGAR egress is blocked in the
> cloud).
>
> **Rev 3 (Jun 27 2026)** — extends the plan with the **daily research → weekly
> decision** architecture worked out across the PM / quant / ML-AI / DevOps /
> backend personas. Three new sections: **§11** the daily research pipeline across
> the two execution planes (GitHub Actions = research engine, Anthropic cloud =
> decision engine); **§12** the synthesis & storage architecture (the per-ticker
> dossier, the raw→curated split, JSON-vs-DB scaling walls + migration triggers);
> **§13** agent temporal context (entry-anchored memory, the `since_entry` /
> `since_last_decision` blocks, invalidation-gated exits, recall cutoffs); and
> **§14** the continuous-research / periodic-decision split (the analyst–PM
> separation that moves agents 2–5 off Wednesday into event-driven weekday
> deep-dives feeding a `bench`, leaving agents 1/6/7 as the Wednesday investment
> committee). The principle threaded through all of them: **use the 4 non-run days
> to accumulate deterministic evidence and pre-digest slow-moving context — NOT to
> manufacture more LLM opinions.** Universe expansion (100 → ~400 names) is approved
> *in principle* but **gated on the §8 fundamental-coverage fix** — a wider pool of
> momentum-only names is just more ways to lose.
>
> **Rev 3.1 (Jun 27 2026, review pass)** — holistic five-lens critical review of the
> Rev 3 plan. Adds **§15** (Observability & the Data-Quality Gate — the layer that
> protects the year-end verdict from being confounded by silent data degradation),
> **§16** (Test & chaos plan — every June failure reproduced as a now-caught test),
> and **§17** (the P0/P1/P2 correctness-fix punch-list + uncovered edge cases).
> **One inline correction:** the §11.4 SEC check was a *delta* ("coverage drop >10%
> WoW") that would NOT have caught the actual June bug (a steady 28% coverage —
> nothing dropped) → changed to an **absolute coverage floor**. Headline review
> principle: **every decision carries a data-quality provenance stamp, and the
> harness records data quality as a covariate — a run below the floor is *excluded*
> from the success/kill evaluation, never silently averaged in.** That is what lets
> December produce a *trustworthy* verdict (win, lose, or "data was degraded — N/A")
> instead of an excuse.
>
> **Rev 3.2 (Jun 27 2026, governance pass)** — adds the institutional **governance
> layer** (§18) modelled on professional-firm practice, and the standalone
> foundational artifacts that ship with it. Owner directive: **use best practice
> everywhere, even where it would be "over-building" at this size.** New: the
> Investment Policy Statement as the single source of truth; the Three Lines of
> Defense; SR 11-7 Model Risk Management with a model register + decommission
> criteria; counterfactual rejected-name tracking and Brinson attribution in the
> harness (§7.5); strategy/parameter change-control; a market-wide crisis safe-mode;
> and a quarterly investment review. This whole change is **built and released as one
> pod** — see the document map below.
>
> **Rev 3.3 (Jun 27 2026, second governance review integrated)** — folds in the
> remaining professional-practice gaps from the holistic review: time-weighted returns
> + broker-1099 reconciliation + risk-adjusted success criterion + the Fundamental-Law
> breadth ceiling + the three-clocks verdict-scope reconciliation (§7.6); a
> capital-graduation ladder and terminal action ([IPS.md](IPS.md) §3.5–3.6);
> data/vendor integrity with outlier cross-validation (§18.8); operational resilience —
> incident lifecycle + BCP/vendor-risk register (§18.9); forward scenario stress
> (§18.5); and the M8 quant composite reclassified as the **non-decommissionable
> baseline** (failing it means "go passive," not "remove it"). Owner decisions this
> pass: **live book trades real (insignificant) capital from day one, measured on the
> paper arms** (IPS §3.4); **model pinning = record + governed adoption, not freeze**
> (§18.4).

---

## Related documents (this release pod)

This redesign ships as a single coherent pod. The documents are mutually linked:

| Document | Role | Links to |
|----------|------|----------|
| **[IPS.md](IPS.md)** | **Foundational artifact** — the single source of truth for every limit, constraint, and policy. Governs all code and prompts. | → this plan (design), → [MODEL_REGISTER.md](MODEL_REGISTER.md) (§10) |
| **[STRATEGY_REDESIGN_PLAN.md](STRATEGY_REDESIGN_PLAN.md)** (this doc) | The design & build plan: cadence, pipeline, storage, memory, analyst split, observability, governance | → [IPS.md](IPS.md), → [MODEL_REGISTER.md](MODEL_REGISTER.md) |
| **[MODEL_REGISTER.md](MODEL_REGISTER.md)** | The MRM model inventory — each agent/quant as a governed model with validation status + decommission criteria | → [IPS.md](IPS.md) §10, → this plan §18.3 / §7 |
| **[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)** | The build & rollout plan — phases sequenced by dependency/risk, offline-first, live path gated hardest | → all of the above |
| `policy.yaml` (to build) | Machine-readable mirror of IPS Appendix A; `guardrails.py` + prompts derive limits from it | ← [IPS.md](IPS.md) Appendix A |

> **Build/release note:** because limits will be *derived* from `policy.yaml` (IPS
> Appendix A) rather than hard-coded, the pod's first implementation step is to extract
> the existing `guardrails.py` constants into `policy.yaml` and prove parity — no
> behavior change, just single-sourcing. Everything else builds on that.

---

## 0. TL;DR

1. **The system has no demonstrated edge.** Own backtest: quant-only **−0.03% vs
   SPY +8.77%**. 72% of names are scored on **momentum + volatility only** (no real
   fundamentals). Optimizing *exit* logic before proving the *entry* has alpha is
   premature.
2. **The system's own backtest already says daily is value-destructive**: "monthly
   rebalance (+$4,185) vastly beats daily (−$242) — churn is value-destructive."
   Yet it runs **daily**. The three churn symptoms the owner sees are direct
   consequences of daily cadence.
3. **Strategy-identity mismatch is the root cause:** it *enters* on momentum (a
   weeks-to-months signal) but the owner wants to *hold* on conviction (months+).
   These don't compose. The owner has chosen: **not momentum — medium/long-term.**
4. **Two approved moves** (lowest risk, highest leverage):
   - **A. Weekly cadence — a fixed Wednesday rebalance** + a cheap daily
     `risk_watch.py` that routes through the *existing* execution envelope.
   - **B. Switch on the already-built measurement harness** (`calibration.py`) and
     extend it to medium/long-term horizons — **after** fixing the forecast feed,
     which silently stopped logging on Jun 18.
5. **The blocker for the strategy shift:** fundamental data is missing for ~72% of
   names. A medium/long-term fundamental strategy cannot run on data we don't have.
   **Fix fundamental coverage first.**

---

## 1. The Jun 23 2026 run — audit

The daily cycle ran cleanly end-to-end. Mechanics were correct; the concerns are
strategic, not plumbing.

| Step | Result |
|------|--------|
| Pre-flight gate | PROCEED — fresh snapshot (100 tickers, 205-bar min depth) |
| Portfolio | $507.51 total · $300.14 cash (59.1%) · 5 positions |
| Regime | NEUTRAL (confidence 58) |
| Kill switch | Clear |
| Trades executed | **0** |
| Artifacts | Pushed to `main` (06d7e97 health, 0d6d2b7 daily cycle) |

**Why zero trades:**
- PM proposed **BUY JPM** (funded by **SELL VRTX**).
- CRO **vetoed JPM** — adding it would put Financials at ~32% (MS 9.6% + JPM 9.0% +
  AXP 7.9% + BAC 5.1%), breaching the 25% sector cap with high intra-sector
  correlation (BAC/JPM 0.79).
- The **VRTX SELL** was rejected by the good-faith-violation guard (bought Jun 22,
  < 2 trading days earlier).
- Two independent guards both said no → no trades. **The defensive layers worked.**

**Standing health flags:**
- `DEGRADED cash_discipline`: cash 59.1% > 15% ceiling, **17 consecutive runs**.
- `DEGRADED decision_validation`: 1 rejected (the VRTX SELL).

---

## 2. Deeper investigation — what the agent log actually showed

The first-round diagnosis ("quant only likes Financials, candidate pool floods
with capped names") was **wrong**. The agent log disproved it:

- **Top quant names are already diverse:** EQIX 90.7 (Real Estate), UNH (Health
  Care), PLD (Real Estate), DE (Industrials), TJX (Consumer Disc), JNJ (Health
  Care) — all high-momentum, non-Financial, already in front of the PM.
- **The Devil's Advocate rejected 6 of the top 10** (EQIX, UNH, PLD, DE, C, UPS),
  and the bear cases are **substantive and professional-grade** (UNH: CMS rate cuts
  + Optum antitrust; EQIX: hyperscaler vertical-integration cannibalization + debt;
  DE: ag down-cycle contradicting the company's own guidance; PLD: industrial
  vacancy surge; C: chronic ROTCE lag). Not reflexive bearishness.
- **Net-edge + after-tax gates already exist** (`enforce_net_edge`, `cost_model.py`)
  and correctly suppress marginal trades.
- The PM's one BUY (JPM, highest non-rejected composite 89.2) was funded by
  **churning VRTX — bought Jun 19 AND Jun 22** (buy → buy → sell in 3 trading
  days). Both CRO and min-holding guard caught it.

**Reframe:** the 59% cash is **largely the system correctly declining to churn** a
CA top-bracket taxable account into risky momentum names. The bug is not "too much
cash" — it's that we cannot yet tell whether the cash is *disciplined* or *stuck*,
because no agent's signal is being measured.

**Data confirmed during the investigation:**
- Quant factor coverage: **100/100 momentum & volatility available; only 28
  quality, 25 valuation.** 72% of names score on momentum+vol only.
- `sector_of` has **100% coverage** on the watchlist (0 unmapped) — a hard sector
  pre-filter would be safe.
- VRTX trades.csv: BUY Jun 19 (dry_run=True, not filled live), BUY Jun 22
  (dry_run=False, filled). REDUCE flagged Jun 23.

---

## 3. The three owner-identified issues — are they addressed?

> Owner's principle: *"A stock purchase should be done if the company's outlook is
> good in the long run, and shouldn't be sold if there was a small blip. Outlook
> should be at least a few months (if not more), not a few days/weeks."*

| Issue | Status | Detail |
|-------|--------|--------|
| **1. Bought then sold in a few days** | **Partially — wrong horizon** | Blocked mechanically: GFV guard (2 trading days, `guardrails.py:495`) + `enforce_min_holding_period` (**5 trading days**, `guardrails.py:253`). But 5 trading days ≈ 1 week, not "months." It's a fence, not a philosophy — the agent still *generates* the exit. **Risk exits are exempt** and "risk" is loosely defined (a loophole). |
| **2. Sold then re-bought** | **Best-addressed** | `enforce_wash_sale_reentry` blocks re-buying a name sold within **30 calendar days** (`guardrails.py:303`). TJX was blocked (sold Jun 10, BUY rejected Jun 19). Genuinely covers the concern mechanically. |
| **3. Agents lose context** | **Partially — critical hole** | Position Review *does* receive the original entry thesis, anti-thesis, `invalidates_if` conditions, and entry date (`run_position_review_analyst`). Research + PM get `get_ticker_history` + `recently_exited`. But nothing forces the agent to *use* it as horizon discipline. |

### The root cause (NOT addressed)

The Position Review prompt (`analysis.py:148-165`) actively encourages short-term
rotation:
- *"Have catalysts already played out?"* and *"Is there a superior opportunity?"* —
  rotation logic; exit if something looks better **today**.
- *"Never anchor to purchase price"* — good for disposition bias, but leaves **no
  anchor to holding period or thesis horizon** either.
- `recommended_action` is **not gated on `thesis_intact`**. An agent can return
  `thesis_intact: true` *and* `REDUCE` because alpha scored "LOW." That is exactly
  VRTX: entered Jun 22 with an explicit **multi-year** thesis ("2025-2026
  catalysts"), flagged REDUCE / hold=4 / alpha=LOW **the next day**. The exit was
  driven by a daily alpha score, not by any `invalidates_if` condition tripping.

**Conclusion:** the mechanical guards prevent over-trading (a tax control); they do
**not** make the agents reason on a months horizon. The owner's principle is not
what the system enforces.

---

## 4. Critical grade on every proposed item (quant researcher + veteran PM lens)

Two facts dominate: **(1) no demonstrated edge**, **(2) the backtest already says
daily churn destroys value**. Against that:

| # | Item | Verdict |
|---|------|---------|
| **5** | Gate exits on thesis-invalidation, not alpha | **Half-dangerous as-is.** `invalidates_if` are *qualitative LLM triggers* the system **cannot measure** (no real-time fundamental feed). Gating exits on unmeasurable conditions ≈ never exiting except on price/risk = the **hold-your-losers trap**. Sound only AFTER the data layer exists (fork B). |
| **6** | Mandate holding-period / horizon discipline | **Cart before horse.** Holding period should be an *output* of the strategy, not a decree. Mandating "hold months" on a momentum book rides reversals down. |
| **7** | Lengthen min-holding to months | **Partially supported, overshoots.** Backtest supports *less* turnover (monthly ≫ daily); proven sweet spot is **~weekly/monthly**, not "months/years." Blanket long min-hold is harmful for momentum specifically. |
| **1** | Hard sector-cap pre-filter on PM input | **Near-theater at this size.** 5 positions / $507 → dominated by single-name idiosyncratic risk, not sector risk. Correct hygiene, immaterial to returns. Low priority. |
| **2** | DA + signal precision instrumentation | **Most important — promote to #1.** No agent has a *measured* alpha contribution. Caveat: with ~5–20 live decisions, **zero statistical power** yet — set it up now so the clock starts; expect verdicts in months, not weeks. |
| **3** | Reframe cash_discipline → measurement | **Correct, deprioritize.** Cash is a position. With no proven edge, forcing deployment to avoid "cash drag" is how amateurs lose money. Passive observability only. |
| **4** | Investigate VRTX whipsaw | **n=1 — don't fit a process to one trade.** Illustration, not evidence. |

**Rejected outright:** forcing cash deployment or a position-count minimum — it
overrides exactly the discipline that's working and churns a 54%-ST-tax account.

**The uncomfortable PM question:** with **$507** and a 7-agent daily LLM pipeline,
token + maintenance cost likely **exceeds** the expected dollar return. That is fine
*as a research/learning platform* — but then optimize for **measurement and
correctness**, not micro-tuning exits on capital too small and too taxed to matter.
Fund the strategy *after* the process is proven on paper.

---

## 5. Decisions taken

- **Cadence:** move from daily to **weekly, on a fixed Wednesday** (owner's choice,
  Rev 2). Wednesday avoids the Monday post-weekend gap and Friday options-expiry
  noise, and — unlike a rotating weekday — needs no stored rotation state.
- **Strategy identity:** **NOT momentum.** Medium-to-long-term. Owner open to the
  changes that requires (data layer, entry signal, exit logic).
- **Build philosophy:** measure before adding logic; backtest deterministic layers,
  forward-test the LLM; burden of proof is on each agent.

---

## 6. Deliverable A — Weekly cadence (fixed Wednesday)

### 6.1 Why a fixed Wednesday, not a rotating weekday

Rev 1 proposed a triangle-wave rotating weekday to "de-correlate the rebalance from
weekday seasonality." Rejected on review:

- At **one rebalance/week on ~5 positions**, weekday seasonality is statistically
  invisible — the de-correlation benefit is academic.
- The triangle wave is also **non-uniform** (Mon/Fri get half the weight of
  Tue/Wed/Thu over each 8-week period), so it doesn't even de-correlate cleanly.
- It requires anchor/phase math and a `rebalance_state.json` — **stored state and a
  new failure surface** for no real return.

A fixed **Wednesday** is stateless, trivially testable, and side-steps the Monday
post-weekend gap-up/down and Friday options-expiry pinning. The gate just asks
"is today Wednesday (or the catch-up day) and have we not rebalanced this ISO
week?"

```python
# preflight_gate.py — deterministic, NO stored phase math
REBALANCE_WEEKDAY = 2   # 0=Mon … 2=Wed … 4=Fri
```

### 6.2 The cron does NOT change

It still fires every weekday (`* * 1-5`) with the 4 morning retries — that
resilience is what we want *on the rebalance day*. The **gate** decides the mode.
DST handling unchanged.

### 6.3 Extended gate exit-code contract

```
preflight_gate.py:
  0  PROCEED/REBALANCE  → is_rebalance_day AND fresh data AND no rebalance
                          this ISO week                       → run main.py (full 7-agent pipeline)
  30 PROCEED/RISK-WATCH → any other trading day               → run risk_watch.py (SELL-only, on the SAME envelope)
  10 SKIP/RETRY         → stale data / closed market / 529    → retry next attempt
  20 SKIP/DONE          → already rebalanced this ISO week     → stop
```

### 6.4 Two run modes — ONE execution path

- **Rebalance run** (Wednesdays): the current `main.py` pipeline, unchanged.
- **Risk-watch run** (the other ~4 weekdays): new **`risk_watch.py`**. It fetches
  the portfolio, evaluates a **tight, pre-defined risk trigger set** (see §6.7),
  and on a genuine risk event builds a `SELL`/`HOLD`-only decision list. **It never
  generates a BUY and never runs the LLM pipeline.** The book is *reconstructed*
  weekly but *guarded* daily.

  > ⚠️ **Capital-integrity invariant (non-negotiable):** `risk_watch.py` is a
  > *decision generator*, **not a second order path.** It MUST hand its decisions to
  > the **existing** `execute_trades` → `journal.mark_execution_started` →
  > `mark_transactions_live` → `mark_pending_executed` envelope, reusing the same
  > `pending_decisions.json` idempotency stamp, per-order try/except, fills-aware
  > reconciliation, and `git checkout -B main origin/main` push discipline that the
  > daily cycle took a dozen incidents (Jun 9–17) to harden. A parallel "lightweight"
  > order path would fork the audit trail and silently shed every one of those
  > controls. ~50 lines of decision logic on top of the proven path — no new
  > order code.

### 6.5 Backend safeguards (capital integrity)

1. **Once-per-ISO-week idempotency.** Today's envelope guarantees once/day; extend
   to once/week by stamping the ISO week. Gate's exit-20 check becomes "has a
   rebalance `executed_at` landed in this ISO week?" The week-stamp **MUST live on
   `main`** and be read only *after* `git fetch origin main && git checkout -B main
   origin/main` — otherwise it repeats the Jun 17 branch-vs-main double-execution
   vector (a stamp on a `claude/*` worktree branch is invisible to the next
   attempt, which re-runs the whole rebalance). Prefer reusing
   `pending_decisions.json` (already on `main`, already in the envelope) over a new
   `rebalance_state.json`.
2. **Within-week catch-up.** If Wednesday's data is stale all 4 attempts (GitHub
   Actions skipped), the week would silently go un-rebalanced. So:

   ```
   is_rebalance_day = (today.weekday() == REBALANCE_WEEKDAY)
                      OR (today.weekday() > REBALANCE_WEEKDAY    # Thu/Fri this week
                          AND no rebalance executed this ISO week)
   ```

   Wednesday normally, with automatic fallback to the next good day (Thu/Fri) that
   week. **DevOps:** emit a health signal if a whole ISO week is missed (silence is
   a failure mode).
3. **Cross-mode SELL interlock.** A rebalance SELL on Wednesday and a risk-watch
   SELL on Thursday could both target the residual of the same name. `risk_watch.py`
   MUST NOT SELL a ticker already traded this ISO week by the rebalance run (read
   the same week-stamped envelope). Prevents a duplicate-SELL surface.

### 6.6 Testing

`PREFLIGHT_DATE_OVERRIDE` already makes this deterministic. Tests must assert: the
Wednesday selection, the once-per-ISO-week lock, the Thu/Fri catch-up fallback,
that risk-watch **never emits a BUY**, that risk-watch routes through the same
execution/stamp/reconcile envelope as `main.py`, and the cross-mode SELL interlock.

### 6.7 Risk-watch trigger set (was Open Question — now a BLOCKER for A)

"What counts as a genuine risk event" is **not** an open question to resolve after
shipping — `risk_watch.py` leans entirely on this definition to justify a daily SELL
path, and a loose definition reintroduces the exact churn the cadence change exists
to kill. Define it *tightly and mechanically* before any code:

- **Allowed triggers (objective, no LLM):** kill switch active (>20% drawdown from
  peak); a per-position hard stop-loss breached — **−25% from entry (cost basis),
  daily-close basis, no trailing, exempt from the min-hold guard** (decided Jun 27,
  §10.2; harness-tunable); a tripped *quantitative* `invalidates_if` that is
  price/level based and machine-checkable.
- **Explicitly NOT a trigger:** "alpha scored LOW," "a superior opportunity exists,"
  any daily LLM re-rank, or a qualitative thesis re-read. Those are rebalance-day
  decisions only. This closes the loosely-defined-"risk" loophole noted in §3.

---

## 7. Deliverable B — Measurement harness (extend + switch on)

### 7.1 What already exists (~90% built)

- **`calibration.py`**: `log_forecasts` → `score_matured` (next-open entry, **no
  look-ahead**, idempotent) → `agent_scorecard` (**Information Coefficient**,
  p-value, **block-bootstrap** for overlapping horizons, shrinkage). Tested.
- **`forecasts.jsonl`** (schema v2): `{run_id, date, agent, field, ticker, value,
  signal_close, horizon_days, schema}`. Logged signals: `quant.composite_score`,
  `research.confidence`, `devils_advocate.overall_risk_score`,
  `position_review.hold_score`, `earnings.earnings_alpha_score`.
- **`backtest/`**: deterministic event loop, next-open fills, momentum/inverse-vol
  + equal-weight strategies.
- **`performance.py`**: after-tax-vs-SPY (total-return corrected), drawdown, vol,
  Sharpe, beta, net-exposure.

### 7.2 THE GAP (two parts — the second was missed in Rev 1)

1. **Scoring is off.** `score_matured` / `agent_scorecard` are **called only from
   tests** — never in the routine. The harness is built and switched off. (Verified:
   grep finds zero non-test callers.)
2. **The feed itself stopped.** `log_forecasts` *is* wired (`main.py:487`), but the
   last line in `forecasts.jsonl` is dated **Jun 18** — the runs on Jun 19, 22, and
   23 logged **nothing**. So the premise "signal accumulates that nothing scores" is
   half-wrong: it *stopped accumulating* five days before this audit. **Diagnose the
   logging gap FIRST** — wiring `score_matured` against a dead feed produces an empty
   scorecard with a green light, which is worse than no harness. (All 144 existing
   forecasts are also `horizon_days: 21` only — confirming §7.3.2's premise.)

### 7.3 Four concrete additions

1. **Wire scoring into the weekly run** (and ideally risk-watch, which has fresh
   prices):
   ```python
   calibration.score_matured(snapshot)   # matured forecasts → realized fwd returns
   calibration.agent_scorecard()         # IC / hit-rate / p-value → signal_scorecard.json
   ```
   Commit `forecasts.jsonl`, `forecasts_scored.jsonl`, `signal_scorecard.json`.
   Starts the evidence clock immediately.

2. **Multi-horizon — the key change for non-momentum.** Today everything scores at
   **21 days (1mo)**. A medium/long-term signal *should* look weak at 21d and
   strong at the longer horizons — judging it at 21d would wrongly condemn the
   strategy we want. The owner's chosen horizon is **9–12 months** (§10.1), so log
   each forecast at **{21, 63, 126, 189, 252}-day** horizons (the last two = 9mo/12mo,
   the **primary** IC horizon to optimize); produce an **IC curve per agent across
   horizons**. Rising IC at 9–12mo = the medium/long-term alpha we're after; IC that
   decays after weeks = momentum noise. **Power caveat (§7.4): a 252d forecast takes
   ~12 months to mature** — the per-name LLM verdict at this horizon is firmly a
   multi-year / possibly-never result; do not sequence anything behind it.

3. **Beat real baselines, after tax.** Add a portfolio-level after-tax comparison
   vs **SPY (total return)** and **QQQ (total return — REQUIRED; the book is
   tech/mega-cap-tilted, so beating SPY while losing to QQQ is not real alpha,
   §10.2)**, plus **equal-weight top-N** (`backtest/strategies.equal_weight_topn`
   exists), **random-N**, and **buy-and-hold the initial book**. Reuse
   `performance.py`'s after-tax engine. House rule: *a signal is unproven until its
   after-tax return beats equal-weight, SPY, AND QQQ at its native horizon with
   significance.* This baseline set is also the **pre-registered success/kill bar**
   for the "fund it vs. just buy ETFs" decision (§10.3).

4. **Per-agent dollar attribution + quant-only shadow arm.** Tie realized after-tax
   P&L back to the decision (and agent) that caused it → running $ scorecard per
   agent. Run a **paper quant-only portfolio in parallel** (no LLM): if the
   7-agent book doesn't beat the deterministic quant book after tax, the LLM layer
   is costing money — and the scorecard will say so.

### 7.4 Statistical honesty

`agent_scorecard` already emits p-values + shrinkage. Surface an explicit
`NOT_SIGNIFICANT` flag below ~30 matured forecasts / 60 trading days; never let the
routine treat an unproven agent's signal as load-bearing until it clears the bar.

**Be honest about the power math — it is brutal at this cadence.** Weekly rebalance ×
~5–20 positions ≈ **~4 new forecasts/month**. A 126-day horizon means each forecast
takes **~6 months to mature**. Clearing the ~30-matured bar at 126d for a *single*
agent is therefore a **1–2 year (or never)** verdict, not the "longer" Rev 1 implied.
Practical consequence — **do not sequence anything behind the per-name LLM IC**:

- **Quant-only / deterministic comparisons:** decision-relevant in days–weeks
  (backtestable now). These carry the burden of proof.
- **CRO-veto / Devil's-Advocate as risk reducers** ("did vetoed names underperform
  held names"): months.
- **Per-name LLM thesis IC at 63/126d:** 1–2 years, possibly never reaches
  significance at this capital/decision rate. Set it up so the clock starts; expect
  no usable verdict for a long time. The harness must say `NOT_SIGNIFICANT` rather
  than pretend.

### 7.5 Professional-practice additions (Rev 3.2)

Three institutional measurement practices, added so the harness can *explain* returns
and *prove* the AI layer earns its keep — not just report a number.

1. **Counterfactual rejected-name tracking — the highest-leverage, near-free addition.**
   The system rejects far more than it buys (Devil's-Advocate rejects, CRO vetoes,
   names that miss the cut). Professional ICs that *don't* track passed opportunities
   lose the information needed to judge their own process. So log every **rejected /
   vetoed** name with its reason and score it forward at {21,63,126,189,252}d alongside
   the held set. This directly answers *"is the CRO (M7) / Devil's Advocate (M4) a real
   risk-reducer?"* — do the names they killed actually underperform the ones we held?
   The candidate scores already exist; only the logging + scoring is new. Feeds the
   [MODEL_REGISTER.md](MODEL_REGISTER.md) counterfactual KPIs for M4 and M7.

2. **Brinson attribution — explain the return, don't just measure it.** Decompose active
   return vs SPY/QQQ into **allocation** (sector over/underweights), **selection**
   (stock-picking within sectors), and **interaction**, plus a factor-tilt decomposition.
   For the platform's core question this is essential: *"we beat SPY"* could be pure
   sector beta. Only the **selection** term isolates genuine stock-picking skill — which
   is what the LLM is supposed to add. Extends `performance.py`.

3. **Model-register KPIs.** Every harness run writes each model's primary KPI (IC,
   hit-rate, counterfactual) into `signal_scorecard.json`, and the quarterly review
   (§18.6) updates the **Validation status** in [MODEL_REGISTER.md](MODEL_REGISTER.md).
   A model stays `NOT_VALIDATED` (logged but **not load-bearing**) until it clears the
   §7.4 bar; one that degrades goes `ON_WATCH`; one that fails its decommission
   criterion is dropped via §18.4 change-control.

### 7.6 Measurement rigor — methodology, reconciliation, verdict scope (Rev 3.3)

The headline metric (after-tax vs benchmark) decides the whole experiment, so its
methodology must be institution-grade:

1. **Time-weighted returns (TWR).** Compute performance time-weighted so deposits/
   withdrawals don't distort it — this is the root of the documented "deposits inflate
   the peak" bug. Report money-weighted IRR alongside for the owner's actual $
   experience. GIPS-style discipline ([IPS.md](IPS.md) §8).
2. **Reconcile the after-tax claim to reality.** `cost_model`/`tax_lots` produce an
   *estimate*; the **broker's realized P&L / 1099 is authoritative.** Reconcile quarterly
   and at year-end — the headline must tie to broker-reported gains, not an internal
   model alone.
3. **Risk-adjusted, not just return.** Report Sharpe, Sortino, and information ratio vs
   SPY/QQQ; beating benchmark return at materially higher vol is not a win (IPS §3.3).
4. **The breadth ceiling is a structural limit, not just low measurement power.**
   Grinold's Fundamental Law: **IR ≈ IC × √breadth.** At ~4 forecasts/month breadth is
   tiny, so achievable risk-adjusted outperformance is **capped even with genuine skill.**
   The honest December conclusion may be *"the LLM has positive IC but breadth is too low
   to beat SPY after tax"* — decision-relevant for fund/abandon. State it; never mistake a
   small-but-real IC for an investable edge.
5. **Reconcile the three clocks (verdict scope).** Three clocks run: the **12-month
   evaluation window** (§10.3), the **9–12-month holding horizon**, and the **1–2-year
   (or never) validation power** for per-name LLM IC (§7.4). The first 252-day forecasts
   don't mature until ~month 12, so the **12-month verdict rests primarily on the
   quant/shadow arm and shorter horizons, not on validated per-name LLM IC.** State the
   verdict's scope explicitly so December isn't over-claimed.

---

## 8. The shift off momentum — sequencing

The owner has chosen medium/long-term (fork B). **One blocker gates everything:**
the composite is momentum+vol because **fundamentals are missing for 72% of names**
(today: only 25–28% had quality/valuation). A fundamental medium-term strategy
cannot run on data we don't have.

1. **Fix fundamental coverage first — in the right layer.** `SECProvider` (free, no
   key) should supply gross/operating margin + debt/equity for ~all US equities but
   is populating only ~25%. **Aim the diagnosis at the GitHub Actions enrichment
   path, NOT the cloud routine:** SEC EDGAR egress is *blocked* in the Anthropic
   cloud (only Robinhood MCP + Anthropic API are reachable), so the routine never
   calls `SECProvider` at all — fundamentals are baked into `market_snapshot.json`
   by `fetch_snapshot.py` running in GH Actions. The likely culprit is visible in
   the code: `SECProvider._ensure_cik_map` swallows any fetch failure into
   `self._cik = {}` (data_providers.py:183-184), after which **every** ticker lookup
   silently returns `None`. Check the GH Actions logs + that silent-empty-map path
   and the per-ticker `except Exception` swallows.
2. **Re-weight the composite** toward quality + valuation + low-volatility; demote
   momentum to a minor confirm. This is a **deterministic change — backtestable
   today with zero LLM and zero live capital** (the highest-value, lowest-risk item
   in the whole plan; see build order §9). Score the new weighting in the backtest
   *and* forward harness at 63/126d.
3. **Then** exit items 5/6/7 become coherent: hold to *fundamental* invalidation,
   longer min-hold, ignore price blips — because now the *entry* is fundamental
   too. They were dangerous on momentum; they're correct on this.

---

## 9. Build order (re-sequenced in Rev 2)

Rev 1 fronted the slowest, most-blocked work (wire scoring — which depends on a feed
that died Jun 18) and buried the fast, deterministic, decision-relevant work
(composite re-weight + quant-only shadow arm) at the back. Corrected:

1. **Deterministic, backtest-now work — do this first; zero live risk, verdict in
   days:**
   - **Fix `log_forecasts`** (silently broken since Jun 18) so the evidence clock
     actually ticks, then wire `score_matured` + `agent_scorecard` and add 63/126d
     horizons.
   - **Re-weight the composite** toward quality/value/low-vol and **run the
     quant-only shadow backtest** (`backtest/` already exists). This is the real
     alpha lever and needs no LLM and no live capital.
2. **Weekly cadence (fixed Wednesday) + `risk_watch.py` as a decision-generator on
   the existing execution envelope** (once-per-ISO-week idempotency on `main`,
   Thu/Fri catch-up, cross-mode SELL interlock, §6.7 tight risk-trigger set).
   Architecturally dissolves the churn issues.
3. **Fundamental-coverage fix in the GH Actions path → confirm the re-weighted
   composite on real coverage → re-backtest.**
4. **Then** exit logic (items 5/6/7), now coherent with a fundamental entry; the
   §6.7 risk-trigger definition is a prerequisite, already pulled forward into
   Deliverable A.

All behind DEPLOYMENT §7.0 gates (RELEASE_NOTES + expert `/code-review` + tests +
weekend dry-run). Step 1 is pure offline tooling — start there. `risk_watch.py`,
being a *new live SELL path*, warrants `/code-review ultra`, not `high`.

---

## 10. Open questions

### 10.1 Owner decisions — RESOLVED (Jun 27 2026)

- ✅ **Account scale → RESEARCH PLATFORM, not a profit vehicle.** The explicit goal
  is to test *whether an LLM can beat the market after tax* before deciding to fund
  it vs. just buying SPY/QQQ/ETFs. **Consequences (binding on the whole plan):**
  (1) optimize for **measurement and correctness** (Deliverable B) over return
  micro-tuning; (2) the harness **must be allowed to conclude "don't trade — just
  hold the ETF"** — that is a valid, even likely, success-criterion outcome, not a
  failure; (3) the §8 strategy shift is a *harness-tested research question*, not a
  roadmap to auto-ship to live capital; (4) the real $507 book proves **execution
  plumbing**; the **paper / quant-only shadow arms prove strategy** — for the
  "add money?" decision the after-tax-modeled shadow comparison is the more honest
  signal, since $507 is too small to be statistically meaningful on its own.
- ✅ **Horizon → 9–12 months.** Drives: primary IC horizon extends to **~189d / 252d**
  (added to {21,63,126} in §7.3.2 — a 9–12mo signal *should* look weak at 21d); a
  much longer **min-holding floor** (see decision below); and the **1-year tax
  boundary becomes a first-class concern** (see 10.2 — this is the highest-$ item).
- ✅ **Exited-name recall window → 90 days** (confirmed). §13.5 updated.
- ✅ **Universe size → ~400 names** (confirmed), gated on the §8 coverage fix.

### 10.2 Expert decisions taken (owner delegated — Jun 27 2026)

- **Stop-loss (§6.7) → hard −25% from entry (cost basis), evaluated on daily close,
  no trailing, risk-exit exempt from the min-hold guard.** Rationale: for a 9–12mo
  *fundamental* hold the owner explicitly does **not** want to sell on blips, and
  quality large-caps routinely draw down 20–25% intra-year and recover — a −15% stop
  would whipsaw out on noise. −25% is a **catastrophe brake**, not a thesis tool: wide
  enough to ignore normal vol, tight enough that with the 10% max position a single
  name caps portfolio impact at ~2.5%. From *entry* (not trailing) to avoid tax-churn
  on winners. **Harness-tunable** once data exists. The portfolio-level 20%-drawdown
  kill switch is unchanged and independent.
- **Min-holding floor → lengthen from 5 trading days to ~30 trading days (~6 weeks),
  risk-exits (stop-loss, tripped invalidation, kill switch) always exempt.** A 5-day
  floor cannot express a 9–12mo philosophy. This was "harmful for momentum" in §4-item-7,
  but coherent now that the entry is fundamental and the horizon is long. Not a hard
  9–12mo lock (too rigid — never trap capital in a broken thesis); a soft floor that
  stops the weeks-long flip while leaving every risk exit open.
- **Universe admission floor → market cap ≥ $10B AND ~30-day avg daily dollar volume
  ≥ $50M, US-listed common stock + ADRs.** Keeps fractional fills at $5–50 essentially
  slippage-free and the cost model valid; excludes small-caps where the model breaks.
- **Benchmark set → primary SPY (total return, after tax); REQUIRED secondary QQQ.**
  The book is tech/mega-cap-tilted, so beating SPY while losing to **QQQ** is *not*
  real alpha — QQQ is the honest comp and is now mandatory in `performance.py`'s
  scorecard alongside equal-weight-top-N, random-N, and the quant-only shadow (§7.3.3).

### 10.3 Follow-on decisions raised by the Jun 27 answers — ALL RESOLVED

- ✅ **1-year tax boundary → tax-aware hold APPROVED (Jun 27).** 9–12 months sits
  *right on* the short-term (~54%) / long-term (~37%) line at 365 days. A winner sold
  at month 11 is taxed ~54%; held three more weeks past one year, ~37% — nearly **half
  the tax** on the same gain. **Decision:** add a *tax-aware hold* nudge — when a
  position is in gain and within ~30 trading days of its 1-year date, the PM strongly
  prefers HOLD over a discretionary trim (risk exits — stop-loss / tripped invalidation
  / kill switch — still exempt). Guardrail + PM-prompt change, harness-measured.
- ✅ **Rebalance cadence → START WEEKLY, revisit monthly later (Jun 27).** Keep weekly
  Wednesday *evaluation*; the long min-hold + carry-forward default make it
  *"check weekly, trade rarely"* in practice. Revisit a monthly rebalance once the
  harness shows whether weekly still over-trades.
- ✅ **Pre-registered success / kill criteria → APPROVED (Jun 27).** Fixed *before*
  the data arrives so it can't be rationalized post-hoc. **The bar:** over a
  **12-month** window, *fund it (add real capital)* only if the **after-tax** book
  beats **SPY AND QQQ** with statistical significance **AND** beats the
  **quant-only shadow** after tax (else the LLM adds cost, not value — just run the
  cheaper deterministic quant); *abandon / just hold ETFs* if it underperforms a
  SPY/QQQ hold after tax past the window. **Honest caveat:** $507 is too small for the
  *live* book's 12-month result to be statistically meaningful on its own, so the test
  is run primarily on the **paper / 100×-shadow modeled book** (same trades, same
  prices); the live $507 book exists to prove execution plumbing. The harness owns the
  significance test (§7.4); the `NOT_SIGNIFICANT` flag governs until the bar is met.

### 10.4 Resolved earlier

- ~~Fundamental coverage root cause~~ (Rev 2) → GH Actions enrichment path +
  silent-empty-CIK-map swallow (§8.1).
- ~~Risk-watch exit triggers~~ (Rev 2) → tight, LLM-free trigger set (§6.7).

---

## 11. Daily research pipeline — 4 research days + 1 run day (Rev 3)

The weekly cadence (§6) frees Mon/Tue/Thu/Fri from decision-making. Use them to
**accumulate deterministic evidence and pre-digest slow-moving context** so the
Wednesday agents read a small, dense, pre-built dossier instead of a firehose.

### 11.1 The decisive system constraint — two execution planes

There are **two execution environments with different network egress, and they
communicate ONLY through the git repo.** This is network policy, not preference,
and it dictates the entire design.

| Plane | Can reach | Cannot reach | Role |
|-------|-----------|--------------|------|
| **GitHub Actions** | Polygon, SEC EDGAR, FMP, Supabase, **Anthropic API** | — | **Research + synthesis engine** — runs every weekday, commits artifacts |
| **Anthropic Cloud routine** | Robinhood MCP, Anthropic API | **Polygon, SEC EDGAR, Supabase → all 403** | **Decision + execution engine** — reads pre-built artifacts |

**Consequence:** the cloud routine *cannot do research* — it can't reach any data
API. Every byte of research must be produced in GitHub Actions, committed, and read
back from the repo. Because GH Actions *can* reach the Anthropic API, even the cheap
Haiku news-digestion runs there — keeping the cloud routine lean.

> **GitHub Actions = research engine (every weekday). Cloud routine = decision
> engine (reads the dossier).**

### 11.2 What runs each day

| Day | Cloud mode | GH Actions research steps | Touches order path? |
|-----|-----------|---------------------------|---------------------|
| **Mon** | risk-watch (exit 30) | fetch slice A · fundamentals · score · digest · build_dossier | risk_watch only |
| **Tue** | risk-watch | slice B · fundamentals · score · digest · build_dossier | risk_watch only |
| **Wed** | **REBALANCE (exit 0)** | slice C · … · build_dossier | **main.py** |
| **Thu** | risk-watch | slice D · … · build_dossier | risk_watch only |
| **Fri** | risk-watch | slice (held + refresh) · … · build_dossier · **harness scoring** | risk_watch only |

### 11.3 Per-step contract (which API · who reads · where stored · who synthesizes)

| Step | Runs in | API | Reads | Writes (where / format) | Cadence | Consumer |
|------|---------|-----|-------|-------------------------|---------|----------|
| **1. Universe fetch** | GH Actions | **Polygon** (5/min cap) | `universe.json` + cursor | raw OHLCV (Supabase / cache — see §12) | slice/day; **held names every day** | scoring; Wed price |
| **2. Fundamentals** | GH Actions | **SEC EDGAR** + FMP | CIK map, last-filing dates | `fundamentals_store` keyed `ticker→{value,_as_of_filing}` | quarterly (on new filing) | scoring; dossier |
| **3. Factor scoring** | GH Actions | none (deterministic) | snapshot + fundamentals | append `factor_history` `{date,ticker,scores,*_available}` | daily | dossier; persistence; harness |
| **4. Event digest** | GH Actions | **Anthropic Haiku** (cached) | `news` / `ticker_news` | append `events` `{date,ticker,type,summary,url}` (deduped) | daily | dossier |
| **5. build_dossier** | GH Actions | none | ALL raw above + journal | `research_dossier.json` — small, denormalized per-ticker | daily (last step) | **Wed agents read ONLY this** |
| **6. harness scoring** | GH Actions | none | `forecasts.jsonl` + snapshot | `forecasts_scored.jsonl`, `signal_scorecard.json` | Fri (or daily) | humans / future gating |
| **Decision** | Cloud | Anthropic + Robinhood MCP | `research_dossier.json` + `mcp_portfolio.json` | `pending_decisions`, `trades.csv`, `system_health`, append `forecasts.jsonl` | Wed | execution envelope |

**Who synthesizes:** `build_dossier` (Step 5) is the **single synthesis point** — it
collapses the raw firehose into the small dossier. Cloud agents never touch raw data.
That is what prevents Wednesday overload.

### 11.4 Edge cases & the need each run must satisfy

| Edge case | Risk | Mitigation (the run's *need*) |
|-----------|------|-------------------------------|
| GH Actions cron skipped (standing risk) | Wed on stale dossier | 3 staggered crons + dossier `as_of` check in gate → **exit 10 SKIP/RETRY**, never trade blind; manual dispatch fallback by 9:15 AM |
| Cloud fires before research committed | reads yesterday's dossier | gate requires `dossier.as_of == today` AND `built_from_days ≥ 2`; else SKIP/RETRY |
| Polygon 5/min exhausted mid-fetch | partial snapshot | slice ≤125/day; **resumable cursor** `fetch_progress.json` (cache-persisted) — crash resumes at N+1, never refetches |
| **SEC chronic-low coverage** (`data_providers.py:183-184`) | fundamentals silently <30% forever | **ABSOLUTE floor, not a delta** (§15): coverage < 80% of the active universe → DEGRADED + block the strategy shift; empty CIK map → ABORT. A "coverage drop >10% WoW" check **does NOT catch a steady 28%** — the exact June bug — so the floor is absolute. |
| **Per-ticker price staleness** | candidate sized on a 1–4-day-old slice price | dossier carries per-ticker `price_as_of`; **size/execute against live Robinhood MCP quotes** (cloud can reach MCP), dossier price is for ranking/research only (P0-1, §15/§17) |
| **Corporate actions / delisting** | split → corrupted momentum/vol; delisted held name unresolved | assert Polygon history is split/dividend-adjusted; explicit delisting/M&A handler for held names (P0-3, §17) |
| **Malformed `build_dossier` output** | Wed trades on garbage/stale dossier | JSON-schema-validate the dossier; gate ABORTs (SKIP) on invalid; never silently trade a stale one without a DEGRADED flag (P1-5, §17) |
| Haiku digest parse failure | events gap | append-only + dedup; fail rate >20% → health DEGRADED; never blocks pipeline (events are enrichment, not gating) |
| Two research crons same day | duplicate append | idempotency: upsert-by-`(date,ticker)`; `git diff --quiet` skips empty commits (already in `market_data.yml`) |
| Concurrent push race (research vs routine) | rejected push | `git fetch && checkout -B main origin/main` + `pull --rebase` retry before push |
| Fundamentals stale >100d | quality factor on old data | dossier `data_quality.fundamentals_age_days`; honest composite **drops** the factor; no-look-ahead: never use a value whose `_as_of_filing > as_of` |
| Universe expanded, fundamentals not covered | wider momentum-only noise | `build_dossier` admits a name to the candidate set only once `fundamentals_store` covers it |
| Whole ISO week missed | silent un-rebalanced week | health signal if no rebalance `executed_at` lands in an ISO week (silence is a failure mode) |

### 11.5 Workflow changes

| Workflow | Change |
|----------|--------|
| `market_data.yml` | **Evolve into the research pipeline**: keep fetch as Step 1, add Steps 2–6, move earliest cron earlier (≈5:00 AM ET) for the longer crawl, persist `fetch_progress.json` cursor |
| `alert.yml` | add research-failure checks (fundamentals coverage, missed ISO week) |
| `publish.yml` / `health_check.yml` / `keepalive.yml` | unchanged |
| Cloud routine | gate adds mode routing (exit 0/30); `main.py` reads `research_dossier.json` instead of the raw snapshot |

**Capital-integrity invariant (non-negotiable):** the four research jobs write
research artifacts only and contain **zero order code**. The only thing that trades
off-Wednesday is the deterministic `risk_watch.py`. Blast radius of any research bug
is therefore *"degraded dossier,"* never *"unintended trade."*

---

## 12. Synthesis & storage architecture — JSON vs DB (Rev 3)

### 12.1 The raw → curated split (what prevents Wednesday overload)

Two **physically separate** layers:

```
┌─ RAW CAPTURE (big, append-only, agents NEVER read directly) ──────────┐
│  raw OHLCV history          (Supabase / cache — see §12.4)             │ daily
│  fundamentals_store         keyed (ticker → {value,_as_of_filing})    │ quarterly
│  factor_history.jsonl       append {date,ticker,scores}               │ daily
│  events.jsonl               append {date,ticker,event} (deduped)      │ daily
│  forecasts.jsonl            (existing — FIX the Jun-18 break)          │ daily
└──────────────────────────────────┬────────────────────────────────────┘
                                    │  build_dossier (synthesis, Step 5)
                                    ▼
┌─ CURATED DOSSIER (small, denormalized — the ONLY thing Wed agents read)┐
│  research_dossier.json   { ticker → digest (§12.2) }                   │
└────────────────────────────────────────────────────────────────────────┘
```

### 12.2 The per-ticker dossier (the Wednesday agent input)

```jsonc
{
  "ticker": "AVGO", "as_of": "2026-07-01", "built_from_days": ["06-29","06-30","07-01"],
  "price": { "close": 294.23, "change_pct": -0.4 },             // Wed-fresh
  "factors": { "momentum": 78, "quality": 64, "valuation": 41, "lowvol": 55,
               "composite": 71.2, "factors_used": ["momentum","quality","valuation","lowvol"] },
  "persistence": { "composite_7d_mean": 70.1, "composite_7d_std": 2.3, "rank_chg_7d": 1 },  // §3 signal
  "fundamentals": { "gross_margin": 0.74, "op_margin": 0.31, "debt_to_equity": 1.1,
                    "rev_growth_yoy": 0.22, "_as_of_filing": "2026-05-15" },
  "events": [ {"date":"2026-06-30","type":"rating_change","summary":"MS PT raised to $320"} ],
  "earnings": { "next_date": "2026-08-04", "days_until": 34, "imminent": false },
  "history_summary": { "ret_21d": 0.04, "ret_63d": 0.11, "ret_126d": 0.19,
                       "vol_ann": 0.28, "beta": 1.15, "max_dd_126d": -0.14 },
  "last_decision": { /* §13 — entry anchor, present for held + recently-exited */ },
  "since_entry":   { /* §13 — cumulative trajectory, held names */ },
  "data_quality": { "fundamentals_age_days": 47, "factors_fresh": true,
                    "coverage": ["momentum","quality","valuation","lowvol"] }
}
```

**Why this wins:** agents read one small denormalized record per name (not 206 OHLCV
bars + 50 news articles); every record is as-of-dated (auditable + no look-ahead);
`data_quality.coverage` tells Wednesday which factors are real; the firehose stays in
the raw layer.

### 12.3 Is JSON the right store? — the three storage classes

The discriminator is the **access pattern**, not the format.

| Class | Artifacts | Access pattern | Verdict |
|-------|-----------|----------------|---------|
| **A. Small keyed state** | `universe.json`, `pending_decisions`, `portfolio_peak`, **the dossier** | read-whole / rewrite-whole, small | ✅ **JSON — keep forever** (the dossier stays a summary, ~400 names × ~1 KB ≈ 400 KB) |
| **B. Append-only time series** | `forecasts.jsonl`, `factor_history.jsonl`, `events.jsonl` | append a line; later scanned | ⚠️ **JSONL fine now**, breaks on **query** (full scan, no index) |
| **C. Large whole-file blob in git** | `market_snapshot.json` (full 206-bar history) | read-whole / rewrite-whole, **committed daily** | ❌ **JSON-in-git is the wall** |

### 12.4 Where it stops being scalable — the walls + the math

**Class C is the soonest wall and your universe-expansion plan triggers it.**
- Today `market_snapshot.json` = **3.2 MB** (100 tickers × 206 bars). At 400 tickers
  ≈ **13 MB**, at 500 ≈ **16 MB**. It's **committed to git daily** and rewritten
  whole (no delta) → **~4 GB git history/year**, and the cloud routine `git pull`s the
  whole repo every morning. The wall isn't RAM — it's **git bloat + pull latency**,
  hit the moment the universe expands.
- **The fix (the dossier already enables it):** the cloud routine needs the *dossier*,
  not 206 raw bars. Factor scoring (which needs the bars) runs in GH Actions and bakes
  the *results* (`ret_21/63/126`, vol, beta) into the dossier. So **stop committing
  the raw-history snapshot to git daily** — commit only the small dossier (cross-plane
  transfer); push raw history to **Supabase** (GH Actions can reach it). This pushes
  the git-bloat wall out indefinitely.

**Class B — slower, query-bound.** `factor_history.jsonl` at 400 tickers ≈ 60 KB/day
→ ~15 MB/year; append scales fine, but computing 7-day persistence scans the whole
file. The break is the **query** wall (e.g. "IC for agent X at 63d in risk-off"),
not size.

**Class A — never breaks** while the dossier stays a summary. Held-position context
is bounded by the **15-position cap**, not by time (§13).

### 12.5 The governing principle — partition storage by *who reads it*

The cloud-egress constraint tells you exactly what must be a file vs. a DB:

| Who reads it | Must live in | Artifacts | Format |
|--------------|-------------|-----------|--------|
| **Cross-plane** (GH writes → cloud reads) | git, small | dossier, pending_decisions, mcp_portfolio | **JSON — keep** |
| **GH-Actions-only** (GH writes → GH reads for analytics) | anywhere GH can reach | factor_history, forecasts, events, raw history, fundamentals_store | **can move to Supabase Postgres** |
| **Audit ledger** (append-only, reconstructable) | append-only file (+ mirror) | trades.csv, decision_journal | **keep files for auditability** |

Key realization: `factor_history` / `forecasts` / `events` / raw history are **only
ever touched by GitHub Actions** (scoring on write, Friday harness on read). None
need git or the cloud plane — their natural home is **Supabase Postgres indexed on
`(date, ticker, horizon)`**. They're files today purely for simplicity.

### 12.6 Migration triggers (so we don't over-engineer at $507)

Files are genuinely fine **now**. Migrate on a *measured* trigger:

| Migrate this… | When (measurable) | To |
|---------------|-------------------|-----|
| Raw history out of daily git commit | committed daily JSON > 5 MB **OR** `.git` > 500 MB **OR** cloud `git pull` > 30 s | Supabase + dossier-only commit |
| A `.jsonl` time series | file > ~50–100 MB **OR** a 2nd full-scan query appears **OR** a hot-path read > 1 s | **SQLite** (one file, indexed, ACID) → **Supabase Postgres** when multi-writer |
| `fundamentals_store` | when point-in-time *as-of* queries across quarters are needed | Postgres keyed `(ticker, filing_date)` |

Progression: **JSON → JSONL → SQLite → Postgres/Supabase**; the trigger is always
*"you need a subset, not the whole thing."* SQLite is the underrated middle step —
carries factor_history/forecasts to thousands of tickers before Supabase is needed.

---

## 13. Agent temporal context — entry-anchored memory (Rev 3)

The owner's requirement: **a weekly run must not be a sandbox.** An agent selling a
name must know why it was bought and what has changed since — at 1 week or 6 weeks.

### 13.1 The reframe — what kills the sandbox

> **Default: carry last week's book and theses forward as the prior. The agent must
> justify a *change*, not re-justify *holding*.**

Today each run effectively starts blank — the quant re-ranks everything, agents form
fresh opinions, and "buy last week / sell this week" happens because the agent is
answering *"what looks good today?"* every Wednesday. The fix is to change the
**question**: from *opinion generator* → *prior updater*. The Position Review agent
is handed its **own prior words** + the pre-committed exit conditions + their current
status, and must contradict itself in writing to sell.

### 13.2 Which history has value to an LLM

| Kind | Value | Why |
|------|-------|-----|
| **Raw price history** (210 bars) | **Low / net-negative** | quant already extracts the signal; raw bars → LLM pattern-hallucination + token burn. Feed *derived* deltas, never bars. |
| **The system's own decision history** | **The memory that matters** | the only thing that makes a run continuous; half-built in `decision_journal.json` |
| **The *delta* since the last decision** | **The actual answer** | not "all history" — *"what is different, and did any exit condition trip"* |

### 13.3 Two reference points, different lifetimes (the cutoff answer)

A sell decision is **always** evaluated as *entry-anchor vs. now* — never
*last-week vs. now*. So a name bought 6 weeks ago is judged **identically** to one
bought last week: same anchor, same invalidation check; only `days_held` and the
trajectory length differ. The holding period never changes *which* question is asked.

| Reference | What | Lifetime |
|-----------|------|----------|
| **Entry anchor** | original thesis + invalidation conditions + inputs-at-entry | **lives as long as the position is held** (1 week or 8 months) |
| **Trajectory** | path since entry (returns, events, factor drift) | cumulative summary (no cutoff) + rolling ~6-week detail |

**The realization that dissolves the cutoff worry:** you hold ≤ **15 positions**, so
full entry-anchor context is bounded by *position count, not time* — ~3–4 KB/name ×
15 ≈ **50 KB total**. A name held 8 months costs the same as one held a week
(trajectory is *summarized*, not stored raw). There is **no time cutoff on a held
position** — that's the whole point of "not a sandbox."

### 13.4 The data — written at decision, diffed at review

At **decision time** persist alongside the journal entry (append-only):

```jsonc
"last_decision": {
  "date": "2026-06-22", "action": "BUY", "weight": 0.07,
  "thesis_quote": "multi-year hold; 2025-26 catalysts",
  "invalidations": [
    {"text": "Mounjaro rev growth < 25% YoY", "type": "MEASURED", "field": "rev_growth_yoy"},
    {"text": "pipeline readout fails",          "type": "NARRATIVE"} ],
  "inputs_at_decision": { "composite": 71, "rank": 4, "price": 412, "gross_margin": 0.86 }
}
```

`build_dossier` computes, for every held + recently-decided name:

```jsonc
"since_entry": {
  "days_held": 42,
  "return_since_entry_pct": -3.1,
  "max_dd_since_entry_pct": -6.2,
  "composite_drift": -2,                          // 71 → 69: noise, not a regime change
  "persistence_7d": { "mean": 70, "std": 2 },     // stable all week → a blip, not a trend
  "material_events_since_entry": [],              // ranked, deduped (full list in events.jsonl)
  "invalidation_status": [
    {"text": "Mounjaro rev growth < 25% YoY", "status": "INTACT", "evidence": "no new 10-Q since entry"},
    {"text": "pipeline readout fails",          "status": "INTACT", "evidence": "no event"} ],
  "fundamentals_changed": false
}
```

### 13.5 The three populations & recall cutoffs

| Population | What agents see | Recall cutoff |
|-----------|-----------------|---------------|
| **Held (≤15)** | full entry anchor + `since_entry` summary + last ~6 weeks event detail | **none while held** — bound by summarization + 15-cap |
| **Exited** | prior outcome + re-entry warning | **90 days** (confirmed Jun 27, §10.1; ≥ 30-day wash-sale; covers churn) |
| **Candidate (never held)** | current dossier + prior-decision outcome (`get_ticker_history`) | n/a |

> **Governing principle: cut off on *recall* (what agents see), never on *storage*
> (what the system keeps).** Storage is append-only and forever — the harness needs
> the full history to judge whether held theses paid off at 63/126d. The cutoff is
> only about what's put *in front of the agent*.

### 13.6 The exit gate (directly fixes the VRTX whipsaw)

> **A SELL requires a tripped invalidation OR a real measured change — never a daily
> alpha re-rank.** `recommended_action` is gated on `thesis_intact == false` OR
> `invalidation_tripped == true`. "Alpha scored LOW today" is no longer a permitted
> exit reason. The VRTX failure (REDUCE the day after entry on `alpha=LOW`) becomes
> structurally impossible. This makes §4-item-5 ("gate exits on invalidation") *safe*
> — but only once the §8 data layer can actually evaluate MEASURED conditions.

### 13.7 Failure modes to respect

- **Measured vs. narrative invalidations.** Tag each. A MEASURED condition flips only
  when the data layer shows it — `build_dossier` computes the status; the LLM does
  **not** get to assert it tripped (same pattern as the existing earnings-date
  fabrication guard). NARRATIVE conditions need LLM judgment but must **cite an
  event**. *Until §8 coverage exists, most MEASURED conditions stay UNKNOWN* — which
  is exactly why invalidation-gated exits are "half-dangerous" today (§4-item-5).
- **Anchoring / disposition (the opposite failure).** Showing "you own this" risks
  the agent refusing to admit a mistake (hold-your-losers). Resolution: continuity is
  enforced through the **pre-committed invalidation framework**, not through
  ownership — the agent holds because no condition it set *at entry* tripped, not
  because it's anchored. Pre-commitment at entry + delta-check at review = neither
  sandbox nor sunk-cost. Pair with the §6.7 hard stop-loss so "thesis intact" can
  never become "hold a name down 40%."
- **Thesis going stale.** Anchoring to entry doesn't trap you — the anchor asks *"is
  the original thesis still the reason to hold?"* If it's now irrelevant, that is
  itself an invalidation → exit. But **revising** a thesis mid-hold must be an
  explicit, logged `thesis_update` event with a fresh invalidation set — never silent
  drift. (The harness should later check whether revised theses underperform
  originals — goalpost-moving is a measurable failure mode.)

### 13.8 Build-order placement

Slots into §9 as part of the dossier work (after the data layer):
1. §9-step-1/2 (fix `log_forecasts`, fundamentals coverage) — prerequisite; MEASURED
   conditions are un-checkable without coverage.
2. §9-step-3 `build_dossier` — add `last_decision` persistence + `since_entry` diff.
3. Position Review prompt rewrite (carry-forward framing + invalidation-gated
   `recommended_action`) — forward-tested, behind DEPLOYMENT §7.0 gates.

---

## 14. Continuous research / periodic decision — the analyst–PM split (Rev 3)

§11–13 spread *data collection and synthesis* across the week. §14 spreads the
*analytical* work too: a research analyst runs continuously through the week and
feeds a **bench** of researched ideas, so Wednesday becomes a warm allocation meeting
over pre-researched names instead of a cold-start of 20.

### 14.1 Why — this is how professional firms are structured

The defining fact of a real fund: **research is continuous and asynchronous; the
decision is periodic and synchronized.** Two jobs, two cadences.

- **Analysts** run a coverage universe continuously, **event-driven** (dig in when a
  catalyst fires — earnings, a filing, a price move, a new idea), and maintain a
  standing **conviction list / bench** (thesis + target + "what would change my
  mind"). They *pitch ideas*.
- **The PM** does not re-research every name each rebalance; it *consumes* standing
  research and makes the **capital-allocation** decision (sizing, risk budget, when to
  act) — inherently a point-in-time, whole-book act. It *allocates*.
- **Investment committee / rebalance** is the synchronized weekly checkpoint where
  research becomes positions. Quant shops mirror this: signal research is continuous
  and offline; the production system rebalances on a schedule; researchers never
  touch live capital.

The invariant every firm holds: **continuous research · periodic synchronized
decision · research never directly pulls the trigger.** The Wednesday big-bang
(all 7 agents cold) violates it; this section fixes it.

### 14.2 The reframe — it DE-loads Wednesday, doesn't add to it

The 7 agents already split along the firm's org chart. The change is to run the
analyst agents *when their catalyst fires* and the decision agents *at the weekly
checkpoint* — not to add a fourth pipeline.

| Agent | Role | New cadence |
|-------|------|-------------|
| **2. Research Analyst** | per-name thesis | **Continuous** — event-driven weekday deep-dive |
| **3. Earnings/Catalyst** | per-name events | **Continuous** |
| **4. Devil's Advocate** | per-name bear case | **Continuous** |
| **5. Position Review** | per-holding monitoring | **Continuous** — re-examine a holding when its `since_entry` deltas trip a threshold |
| **1. Regime** | portfolio-level | **Wednesday** (the investment committee) |
| **6. Portfolio Manager** | allocation | **Wednesday** — needs whole book + current prices |
| **7. CRO** | risk veto | **Wednesday** |

Agents 2–5 *are* the analyst function; 1/6/7 *are* the IC. Wednesday's per-ticker
agents (2–5) shrink to **validate/refresh** the bench against current price rather
than originate from scratch. Likely **net-neutral-to-lower token cost**: deep research
on a few event-triggered names beats shallow research on twenty cold ones.

### 14.3 The two non-negotiable boundaries

1. **The analyst produces conviction NOTES, never a trade proposal.** A trade proposal
   is a decision artifact; generating one Tue *and* Wed yields two time-skewed decision
   sets and quietly moves decision-making back to daily. Keep the analyst in the *idea*
   business (thesis, conviction, entry level, invalidations → the bench); the PM turns
   bench ideas into sized trades on Wednesday. Analysts pitch; the PM allocates.
2. **Deep-dives are event-driven, not arbitrary.** Don't deep-dive random names. The
   dossier's factor-change + event deltas (§11/§13) **are** the analyst's work queue:
   "these names had a material fundamental change / big move / new filing, or (for
   holdings) a `since_entry` threshold trip → deep-dive them." The deterministic layer
   decides *what's worth researching*; the LLM does the *researching*.

### 14.4 The bench — artifacts & contract

```jsonc
// bench.json — the living conviction list (Class-B, GH-Actions-only, §12)
{
  "as_of": "2026-07-01",
  "ideas": {
    "ANET": {
      "conviction": 78,                          // analyst score, 0-100
      "thesis_quote": "AI-backend switching share gain; 2026-27 datacenter capex",
      "entry_level": { "max_price": 95.0, "basis": "20x FY27 FCF" },  // FAST-moving — re-checked Wed
      "invalidations": [
        {"text": "hyperscaler capex guide cut > 15%", "type": "NARRATIVE"},
        {"text": "gross margin < 60%",                "type": "MEASURED", "field": "gross_margin"} ],
      "researched_at": "2026-06-30", "catalyst": "Q2 capex commentary",
      "status": "ready"                          // ready | watching | stale
    }
  }
}
```

- Append-only `research_notes.jsonl` records every deep-dive (full reasoning, audit).
- `bench.json` is the denormalized current conviction list the Wednesday PM reads.
- **Slow vs fast split (quant):** the `thesis_quote` ages slowly; `entry_level` ages
  fast. Wednesday re-checks `entry_level` against the current quote — a 3-day-old
  thesis is fine, a 3-day-old entry price is not.

### 14.5 Where it runs

- The deep-dive analyst runs as a **GitHub Actions step** (needs only the dossier +
  Anthropic API, both reachable there) — keeps the cloud routine lean and off the
  critical path.
- **Off the order path entirely.** The analyst writes notes, never trades. The only
  thing that trades off-Wednesday stays the deterministic `risk_watch.py`. A research
  bug degrades a note, never fills an order.
- Wednesday's PM `user_msg` gains the bench; the gate's freshness check extends to
  `bench.as_of`.

### 14.6 Failure modes & the honest caveat

- **Continuous research must not become continuous decision.** The failure mode is the
  bench becoming a de-facto trade list the PM rubber-stamps — daily trading in a
  costume, eaten by the ~54% ST tax. Research updates continuously; the trigger pulls
  weekly. Guard the §14.3 boundary.
- **Measure it — don't assume alpha.** This is exactly the LLM component that *feels*
  valuable and may contribute zero. The harness (§7) must answer: do high-conviction
  bench names outperform at 63/126d? Is staged deep research better than cold Wednesday
  research? Set it up to be falsified — the system still has no demonstrated edge, and a
  richer research process is a *quality* story, not yet a *returns* story.
- **Stale-entry guard.** Acting Wednesday on a Thursday entry level without re-checking
  price is a look-ahead-in-reverse error; §14.4's slow/fast split closes it.
- **Don't cargo-cult the org chart.** Adopt the *separation* (free, correct); keep the
  analyst lightweight and event-triggered. A 50-analyst fund's process does not
  transplant onto a $507 account — let the harness decide whether the deep-dive analyst
  earns its tokens.

### 14.7 Build-order placement

After the dossier + temporal-context work (§9-step-3, §13), since the deep-dive queue
depends on the dossier's event/factor deltas and the bench depends on `build_dossier`:
1. `bench.json` + `research_notes.jsonl` contracts; event-driven deep-dive queue from
   the dossier deltas.
2. Move agents 2–5 to the weekday GH-Actions analyst step; reduce Wednesday 2–5 to
   refresh/validate.
3. Wire the bench into the Wednesday PM; extend the freshness gate to `bench.as_of`.
4. Instrument bench conviction in the harness (§7) — the burden of proof is on it.
   All behind DEPLOYMENT §7.0 gates; forward-tested, not assumed.

---

## 15. Observability & the Data-Quality Gate (Rev 3.1)

**The problem this section exists to solve:** at year-end (§10.3) the verdict is
*"did the after-tax book beat SPY/QQQ/quant-shadow?"* If data quality silently varied
run-to-run — 80% fundamentals one week, 30% the next, a dead event feed for two weeks
— a bad result is **confounded**: you cannot tell a losing *strategy* from a *starved*
one. The scattered failure notes elsewhere in this plan are necessary but not
sufficient; this section makes data integrity a **first-class, measured, gating**
concern so December produces a trustworthy verdict instead of an excuse.

### 15.1 The governing principle

> **Every decision carries a data-quality provenance stamp, and the harness records
> data quality as a covariate. A run below the data-quality floor is *excluded* from
> the success/kill evaluation — never silently averaged in.**

Concretely: every `forecasts.jsonl` row and every `pending_decisions.json` envelope
carries the run's `data_quality_score` + `data_quality_report` hash. The §7 harness,
when it computes the year-end after-tax comparison, **partitions by data quality** and
reports the exclusion rate. The acceptable December outcomes become: *"underperformed
on clean data"* (real, act on it), *"outperformed on clean data"* (real, fund it), or
*"38% of runs were data-degraded and excluded → verdict N/A, fix the pipeline"* — all
trustworthy. A confounded "it didn't work" is **not** an acceptable outcome.

### 15.2 `data_quality_report.json` — written every run, logged as a time series

Hard floors. **Absolute, not delta** (the headline review fix — a delta check missed
the June 28%-coverage bug because nothing *dropped*):

| Metric | DEGRADED below | ABORT below |
|--------|----------------|-------------|
| Universe fetched vs. expected | 95% | 80% |
| Min history depth (bars) | 60 | 22 |
| **Fundamentals coverage (absolute)** | **80%** | — (blocks strategy shift §8, not the run) |
| Quality / valuation factor coverage | 80% | — |
| Events digested vs. tickers-with-news | 70% | — |
| Haiku digest parse-success rate | 80% | — |
| Forecast-feed last-write age | > 1 run | > 3 runs |
| Dossier `built_from_days` | 2 | 0 |
| Dossier schema valid | — | invalid → ABORT |
| Any NaN/Inf in a numeric field | any → DEGRADED | — |
| Token spend (GH LLM) vs. weekly baseline | > 2× | — |

The report is committed every run and **append-mirrored to a `data_quality_history`
time series** so slow drift (coverage creeping 85% → 60% over a month) is visible
*before* it is a crisis.

### 15.3 The health/alert matrix — success / failure / MISSING per flow

**Silence is a failure mode.** Every flow must alert on *not running*, not only on
erroring. Extends `system_health.json`; the existing `alert.yml` (opens/closes a
GitHub Issue on a health push) is the channel.

| Flow | Success signal | Failure signal | **Missing signal (the silent one)** |
|------|----------------|----------------|-------------------------------------|
| Universe fetch | snapshot committed, ≥95% | partial / empty | **no `chore: market snapshot` by 9:15 AM ET** |
| Fundamentals crawl | coverage ≥ 80% | empty map / floor breach | **`fundamentals_store` mtime ≠ today** |
| Factor scoring | factor_history gained N rows | all-50 / NaN | **no factor_history row dated today** |
| Event digest | events appended | parse-fail > 20% | no events on a high-news day |
| build_dossier | valid dossier, as_of today | schema invalid | **dossier mtime ≠ today** |
| Forecast logging | forecasts.jsonl grew | — | **no new forecast line (the Jun-18 bug) — its own dedicated alert** |
| Harness scoring (Fri) | scorecard updated | — | **no scorecard update in 8 days** |
| risk_watch (daily) | ran, evaluated triggers | exception | **no risk_watch health row for a weekday** |
| Rebalance (Wed) | `executed_at` this ISO week | abort / full veto | **no rebalance `executed_at` in a full ISO week** |
| Cloud↔GH sync | dossier as_of == today | — | gate exit-10 fired all 4 attempts |
| Supabase raw-history write | row upserted | egress 403 / size limit | write age > 1 day |

### 15.4 The heartbeat (dead-man's switch)

The worst failures produce **no error at all** — the Jun-11 silently-skipped cron, the
Jun-18 dead feed. A standalone scheduled workflow runs late each weekday and asserts
that **every expected daily artifact exists and is dated today** (snapshot,
fundamentals_store, factor_history, dossier, forecasts, the day's health row). Any
missing artifact → `alert.yml`. This is the backstop that catches the failure class
the per-flow checks can't, because a flow that never ran writes no failure.

### 15.5 Weekly pipeline-integrity digest

A Friday summary (committed + optionally surfaced) of the week's
`data_quality_history`: coverage trend, parse-fail trend, any DEGRADED/ABORT runs,
token spend, and the harness exclusion-rate-to-date. One glance answers *"is the
machine that produces my year-end verdict healthy?"*

---

## 16. Test & chaos plan (Rev 3.1)

The mandate: **at year-end the failure reason must never be "a flow silently broke."**
The chaos suite (§16.4) is the class of test that enforces it — each historical
failure is reproduced and asserted to now trip a loud signal.

### 16.1 Unit

Absolute coverage-floor gate (not delta); per-ticker price-staleness detection;
`formula_version` continuity guard; multi-horizon `score_matured` idempotency keyed
`(forecast_id, horizon)`; dossier JSON-schema validation; tax-aware-hold per-lot date
logic; corporate-action / split adjustment; holiday-aware ISO-week + Thu/Fri catch-up;
`built_from_days` edge (mid-week new name); risk_watch trigger split (price vs data);
the −25%-stop ∩ wash-sale interaction; data_quality_report floor classification.

### 16.2 Integration

Full research-day pipeline produces a schema-valid dossier from a stubbed snapshot;
cross-mode SELL interlock; gate routing 0/30/10/20 across a week including holidays;
the provenance stamp flows snapshot → dossier → forecast → decision and the harness
can partition by it.

### 16.3 End-to-end (weekend dry-run)

A simulated 5-day week (Mon research → Wed rebalance → Fri harness) on frozen data,
asserting: no double-execution; the data-quality report gates correctly; every flow
emits its three signals; a deliberately-broken flow (kill the SEC fetch) **fails loud
within one run**.

### 16.4 Chaos / negative — reproduce each historical failure as a now-caught test

Inject each silent-failure mode; assert it is **caught and alerted**, not absorbed:

| Injected fault | Historical incident | Must produce |
|----------------|---------------------|--------------|
| Chronic 28% fundamentals coverage | the June bug | DEGRADED + strategy-shift block (absolute floor) |
| Forecast feed stops appending | Jun 18 dead feed | dedicated MISSING alert within 1 run |
| Cron skipped, no snapshot | Jun 11 silent skip | heartbeat alert + gate exit-10 |
| NaN close in snapshot | Jun 16 publish break | DEGRADED + scrubbed, never NaN to Supabase |
| Partial Polygon fetch (rate limit) | 5/min cap | resumable cursor + coverage-floor flag |
| Supabase egress 403 | the 401/403 incidents | OK-classified for cloud, but write-age alert in GH path |
| Malformed dossier | new SPOF (§17 P1-5) | gate ABORT (SKIP), no trade |
| Stale per-ticker price | new (§17 P0-1) | execute re-quotes via MCP, not dossier price |
| Token runaway in GH LLM | new (§17 P2-13) | budget-cap alert |

---

## 17. Critical correctness fixes — Rev 3.1 review punch-list

Tracked findings from the five-lens review. Each names its home section; all are
**OPEN / DESIGN**. Severity by capital/verdict risk.

### P0 — can silently corrupt decisions or the year-end verdict

| # | Finding | Fix | Home |
|---|---------|-----|------|
| P0-1 | Gate checks run-level `dossier.as_of`, but per-ticker candidate prices can be 1–4 days stale (weekly slicing) → PM sizes a BUY on an old price | stamp per-ticker `price_as_of`; **size/execute against live Robinhood MCP quotes**; dossier price = ranking/research only | §11.3 / §12.2 |
| P0-2 | Composite re-weight (§8.2) breaks `factor_history` continuity → persistence_7d & IC curves compare old-formula to new-formula scores | stamp `formula_version` on every row; never compute persistence/IC across a version boundary; re-baseline the harness clock at the change | §8 / §7 |
| P0-3 | Corporate actions unhandled: split → corrupted momentum/vol; dividends → wrong total-return benchmark; delisted/acquired held name unresolved | assert Polygon history split/dividend-adjusted; add delisting/M&A handler for held names | §11.3 |
| P0-4 | Tax-aware hold assumes a single entry date; real positions have multiple lots with different 1-year dates | drive the 1-year boundary off `tax_lots.py` per-lot FIFO dates; confirm tax_lots tracks per-lot acquisition dates | §10.3 |

### P1 — degrades quality/reliability, recoverable

| # | Finding | Fix | Home |
|---|---------|-----|------|
| P1-5 | `build_dossier` is a new SPOF with no integrity gate | schema-validate; gate ABORTs on invalid; no silent stale fallback | §11.4 |
| P1-6 | The −25% price stop conflicts with the 9–12mo "don't sell on blips" philosophy; stop + wash-sale can force a bottom-tick exit and block re-entry | **owner/PM decision:** pure-price stop vs. price-AND-fundamental-invalidation; either way the harness tracks how often the stop fires on names that later recover | §6.7 / §10.2 |
| P1-7 | risk_watch's data-based trigger needs a dossier that may be stale | split triggers: price-based (MCP) always fire; data-based require a fresh dossier or defer to Wednesday | §6.7 |
| P1-8 | Supabase raw-history store adds an egress SPOF (prior 401/403 incidents); cloud can't read it at all | compressed raw-history fallback in `actions/cache`; health-check the Supabase write | §12.4 |
| P1-9 | Multi-horizon maturity bookkeeping unspecified (one forecast now matures at 5 horizons) | key matured records on `(forecast_id, horizon)`; idempotent per horizon | §7.3 |

### P2 — sequencing / completeness

| # | Finding | Fix | Home |
|---|---------|-----|------|
| P2-10 | Build-order circularity: re-weight (step 1) is only validatable after coverage (step 3) | **coverage fix is the true step 1**; re-order §9 | §9 |
| P2-11 | No rollback path for the whole redesign | document revert-to-daily-cycle runbook | §9 / DEPLOYMENT |
| P2-12 | The platform's success *outcome* is undefined — if December says "just hold SPY," what literally happens? | define the terminal action (stop trading / park in SPY / continue paper-only) | §10 |
| P2-13 | No token-cost governance for the new GH-Actions LLM work (~400 names × weekday) | per-run token budget cap + alert (folded into §15.2) | §11 / §15 |

### Uncovered edge cases (add to §16 unit/integration)

- **Holiday-shortened weeks** — Monday holiday breaks `built_from_days ≥ 2`; Wednesday
  holiday forces Thu/Fri catch-up; the market-holiday gate must compose with the
  ISO-week logic.
- **Name enters the universe mid-week** — `built_from_days = 1`, no persistence, no
  fundamentals → mark "insufficient history, candidate-ineligible," never silently
  score it.
- **First run after a fully-missed ISO week** — catch-up + carry-forward prior +
  30-day min-hold clocks must reconcile against the real calendar gap.
- **Partial-sell-then-rebuy** — which entry anchors `since_entry`? (FIFO lot interplay,
  ties to P0-4.)
- **Dossier proposes a candidate already inside its 90-day re-entry block or below the
  $10B/$50M liquidity floor** — filter at `build_dossier`, never let it reach the PM.

> **Resolved by §18 / IPS (Rev 3.2):** P1-6 (stop-loss vs. blip) is now an explicit IPS
> policy — pure −25% price stop as a catastrophe brake ([IPS.md](IPS.md) §7.6/§9), with
> the harness tracking how often it fires on names that recover (§7.5). P2-12 (terminal
> action) is defined in [IPS.md](IPS.md) §3.6 — the owner-confirmed rotation-to-benchmark
> on an Abandon verdict. P2-11 (rollback) and P0-2 (parameter-change continuity) are
> governed by the change-control regime in §18.4.
>
> **Resolved by Rev 3.3 (second governance review integrated):** the after-tax claim now
> uses TWR + broker-1099 reconciliation + risk-adjusted metrics (§7.6 / IPS §8); the
> breadth ceiling and the three-clocks verdict scope are stated (§7.6); the
> capital-graduation ladder and Abandon terminal action are defined (IPS §3.5–3.6);
> data/vendor outlier-quarantine + redundancy (§18.8) and incident/BCP + vendor-risk
> register (§18.9) cover operational resilience; forward scenario stress complements the
> reactive safe-mode (§18.5); and M8 (quant composite) is the **non-decommissionable
> baseline** ([MODEL_REGISTER.md](MODEL_REGISTER.md)).

---

## 18. Governance — IPS · Three Lines of Defense · Model Risk Management (Rev 3.2)

Owner directive: **use professional best practice everywhere, even where it would be
"over-building" at $507.** This section is the institutional governance layer, modelled
on how human-run firms are actually governed (CFA Institute IPS framework; the IIA Three
Lines of Defense; Fed/OCC SR 11-7 model risk management; Brinson attribution; investment-
committee best practice). It ships as part of this release pod.

### 18.1 The Investment Policy Statement is the single source of truth

[IPS.md](IPS.md) is the **foundational artifact**. Every constraint, limit, and policy
lives there once; **code and prompts derive from it** (via `policy.yaml`, mirroring IPS
Appendix A) rather than re-stating it. This kills the drift-bug class the system has
already hit ("the sector limit lived only in the PM prompt"). Where any prompt, code
path, or this plan disagrees with the IPS, **the IPS governs.**

**Build step:** extract the `guardrails.py` constants into `policy.yaml`, prove parity
(no behavior change), then point prompts at the same source. Single-sourcing first;
everything else builds on it.

### 18.2 Three Lines of Defense

| Line | Owned by | Mandate | Independence |
|------|----------|---------|--------------|
| **1st — risk ownership** | Agents 1–6 + quant engine | generate documented decisions | owns the risk |
| **2nd — oversight & control** | CRO (Agent 7) + `guardrails.py` | veto/clamp IPS breaches | **binding controls are deterministic in `guardrails.py`, not LLM judgment** — so the 2nd line holds even if the CRO model shares the PM's blind spots; run the CRO on a different model where feasible to decorrelate |
| **3rd — independent assurance** | harness (`calibration.py`) + `reconcile.py` + data-quality gate + quarterly review | verify decisions, reconcile fills, validate models, report to the **owner** | independent of the 1st/2nd line; grades the process it does not run |

This is the structural answer to the ml_ai warning that *"agents can agree and still be
wrong"*: never let a single LLM be both the decider and its own only check.

### 18.3 Model Risk Management (SR 11-7)

The agents *are* models. [MODEL_REGISTER.md](MODEL_REGISTER.md) is the living model
inventory: each of the 7 agents + the quant composite + the event-digest/analyst models
has a purpose, inputs, **risk tier**, **validation status**, monitored KPI, and a
**decommission criterion**. Governing rules:

- **Default `NOT_VALIDATED`** — a model's output is logged & measured but **not
  load-bearing** until it clears the §7.4 significance bar. The system must be evaluated
  as if an unvalidated model abstained.
- **KPIs** are written every harness run (§7.5); **status** is reviewed quarterly (§18.6).
- **Decommission** a model that fails its criterion — dropping a worthless agent is a
  feature (less cost, less failure surface), governed as a §18.4 change.
- This is the regime that prevents a degraded/worthless agent from silently staying
  load-bearing for a year — the owner's core stated fear.

### 18.4 Strategy & parameter change-control

Code already has the DEPLOYMENT §7.0 gates; **strategy/parameter changes need their own
control** (changing the −25% stop or the composite weights is not a code refactor — it
breaks measurement continuity, P0-2):

- Every parameter lives in IPS Appendix A and changes **only** by amending the IPS
  (effective date, rationale, approver, prior value — IPS §12, Appendix B).
- Each change bumps `policy_version`; the harness **stamps every forecast/decision with
  it** and **never compares across a version boundary unmarked** (resolves P0-2).
- A material change **restarts the relevant evaluation clock** (IPS §3.3).
- **Rollback (P2-11):** reverting to the prior `policy_version` (or to the pre-redesign
  daily cycle) is itself a logged change event with a documented runbook in DEPLOYMENT.
- **Model & prompt versions are change-controlled the same way (closes the silent-drift
  gap).** The underlying LLM version and each agent's prompt are **recorded on every
  decision** and pinned. A provider model update or a prompt edit is **adopted
  deliberately, not silently**: A/B the candidate in shadow on the same inputs, confirm
  it is non-regressive on *this system's* task, then promote and segment the record at
  the boundary. **Pinning = record + governed adoption, NOT freeze** — genuine model
  improvements are captured, just validated before they're trusted, so a provider update
  can never silently confound the measurement or break the JSON parser (a failure this
  system has hit). Detail in [MODEL_REGISTER.md](MODEL_REGISTER.md) §4.1.

### 18.5 Market-wide crisis safe-mode

Per-name stops don't cover a market-wide event. A **safe-mode** trigger (IPS §9 /
Appendix A: index intraday drop > threshold, a trading halt, or a VIX-level breach)
**halts all new BUYs, permits only risk-driven SELLs, and alerts the owner.** Cheap
insurance against the flash-crash / halt scenario the plan otherwise ignores.

**Forward scenario stress (proactive complement, Rev 3.3).** Reactive safe-mode is not
enough; periodically stress the *current* book against historical shock scenarios — **2008
GFC, 2020 COVID, 2022 rate shock** (IPS Appendix A) — by applying those factor/return
shocks to current positions. Report the modelled worst-case drawdown and the two largest
loss contributors, so concentration risk is visible *before* a shock, not just during one.

### 18.6 Quarterly investment review

An automated post-mortem each quarter (the human-firm IC discipline of tracking *closed*
and *passed* decisions): realized after-tax vs SPY/QQQ, Brinson attribution, thesis-
correct rate (from `decision_journal`), **counterfactual rejected-name performance**
(§7.5), model-register status updates, IPS exceptions, and the data-quality exclusion
rate. One artifact answers *"is the process working, and is it being followed?"*

### 18.7 "Over-build" extras (adopted per owner directive)

The owner has chosen to adopt these even though they are largely academic at $507 — build
the framework hooks now so scaling is free later:

- **Transaction-cost analysis (TCA):** log realized fill price vs expected (arrival) and
  vs the cost-model estimate. Negligible at fractional Robinhood size, but it keeps the
  after-tax number and the 100×-shadow honest.
- **Risk budgeting (report, don't enforce):** alongside the capital caps (10%/25%), report
  each position's **risk contribution** (vol- and correlation-weighted) — a 10% position
  in a 60-vol name is a bigger bet than 10% in a 15-vol name. Observability only; the
  hard limits stay capital-based.
- **Separation of duties:** decision / execution / reconciliation / reporting are distinct
  append-only-logged components; the 3rd-line audit (§18.2) is the independence backstop
  for an otherwise single-operator automated system.
- **Investment-memo discipline:** the `decision_journal` entry is treated as the formal
  *investment memo* / case file — thesis, anti-thesis, invalidations, and (new) recorded
  **dissent** (CRO concerns even when not vetoing; PM rationale when overriding a DA
  reject) — so every decision is reconstructable and the dissent is auditable.

### 18.8 Data & vendor integrity (Rev 3.3)

Single-source data is a silent-corruption risk (the NaN-print incident, the SEC empty-map).
- **Outlier cross-validation:** beyond the NaN guard, flag implausible moves (a one-day
  move > the IPS `price_outlier_quarantine_pct` with **no** corporate action = a suspect
  print, not a real move) and **quarantine** the value rather than scoring on it. Extends
  P0-3 (corporate actions).
- **Vendor redundancy for critical inputs:** a second sanity source (or a bounded
  cross-check) for prices; fundamentals already have a provider chain (SEC + FMP).
- **Vendor-risk register:** Polygon, SEC EDGAR/FMP, Robinhood MCP, Anthropic, Supabase are
  all third parties that can rate-limit, change, or deprecate. Track each with its failure
  mode, blast radius, and fallback (feeds §18.9).

### 18.9 Operational resilience — incident management & BCP (Rev 3.3)

- **Incident lifecycle:** detect → triage (severity **P0–P3**) → resolve → **blameless
  post-mortem** → preventive action. The alert system (§15) is detection; this adds the
  lifecycle and a tracked incident log.
- **Business continuity / key dependencies:** a documented response for the outage or
  **deprecation** of each critical vendor (§18.8) — Anthropic routine deprecation,
  Robinhood MCP change, Polygon outage, Supabase block. Define RPO/RTO against the
  existing DR posture; the git repo + append-only logs are the recovery substrate.
- **Key-person dependency:** the owner is the sole fiduciary/approver. Document a fallback
  so a missed quarterly review or amendment window **degrades gracefully** (system holds /
  safe-mode) rather than drifting ungoverned.

### 18.10 Build-order placement

1. **Single-source the limits first** — `policy.yaml` from IPS Appendix A, prove parity
   (§18.1). Zero behavior change; unblocks everything.
2. Stand up [MODEL_REGISTER.md](MODEL_REGISTER.md) + wire the §7.5/§7.6 measurement
   (counterfactual, Brinson, TWR, risk-adjusted, 1099 reconciliation) — pure offline, zero
   live risk.
3. Change-control plumbing (`policy_version` + model/prompt-version stamping, §18.4).
4. Safe-mode + forward scenario stress (§18.5); data/vendor integrity (§18.8);
   incident/BCP (§18.9); the quarterly review job (§18.6).
   All behind DEPLOYMENT §7.0 gates; governance artifacts are version-controlled and
   reviewed quarterly.
