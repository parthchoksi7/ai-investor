# Plan — Separate the beta decision from the alpha decision

**Status:** PHASE 1 SHIPPED (2026-08-22, PR #37). **PHASE 2 BUILT AND VALIDATED
2026-08-22 — held on branch `feat/phase2-beta-neutral`, NOT merged.** The harness says
it must not ship alone; Phases 2 and 3 are now a single unit (see §5). The B2 coverage
blocker was diagnosed 2026-08-22 as a measurement artifact and lifted (see §4).
**Type:** deterministic signal-layer + guard-chain change → touches the live
candidate-selection composite and portfolio construction → **`FORMULA_VERSION` bump at
Phase 2** and **`/code-review ultra`** for Phases 3, 4, 6, 7.
**Owner decisions (2026-08-22):** objective is **maximum after-tax dollars**; **single
names only** — no index ETFs (IPS §5 unchanged); target portfolio beta **0.6–0.8**;
automatic sleeve reduction permitted **with a floor**; capital increasing **$500 → $1,000**.

---

## 1. Goal

Stop the stock-selection model from silently setting the book's market exposure.

Today those two decisions are fused and neither is chosen. A composite intended to rank
expected return is in fact ranking market sensitivity, and a language model is in fact
setting the cash level — which, because cash carries beta 0, is itself a market call.
The result is a book with **no measured alpha and an unintentional short-beta position**.

Split them: a deterministic layer that decides market exposure on purpose, and a bounded
sleeve whose capital is a function of its own measured after-tax contribution.

---

## 2. The finding (2026-08-22, grounded in the live snapshot)

**`composite_score` correlates −0.653 with beta across the universe** (Spearman −0.592,
n=100). Mean beta by composite quintile is monotone:

| Composite quintile | Mean beta |
|---|---|
| Q1 — worst 20 | **+2.41** |
| Q2 | +1.05 |
| Q3 | +0.30 |
| Q4 | +0.54 |
| Q5 — best 20 | **−0.04** |

Driver decomposition (correlation vs beta): `volatility_score` **−0.737**,
`valuation_score` **−0.471**, `momentum_score` −0.071, `quality_score` −0.008. The two
beta-loaded factors carry **0.50 of `FACTOR_WEIGHTS`**. `volatility_score` is a
near-perfect monotone transform of raw volatility (Spearman **−0.999**) — a quarter of
the "4-factor model" is literally *rank by inverse vol*.

Consequences measured on the live book (2026-08-20): stocks-only beta **−0.172**,
portfolio beta **−0.141** with 18.1% cash. Six of nine holdings carried negative beta
(EOG −1.01, CB −0.94, CVX −0.84, JNJ −0.81). Universe mean is **+0.864**.

### Backtest arms — one consistent snapshot, 205 sessions, SPY +9.51% price return

| Arm | Return | After tax | A/T alpha | Beta | Turnover |
|---|---|---|---|---|---|
| Live shape — top 8, inverse-vol, 21d | 4.00% | 4.00% | **−5.51%** | — | 4.62× |
| Top 13, equal weight, 63d | 10.83% | 10.42% | +0.91% | 0.14 | 3.20× |
| Top 13, equal weight, **252d** | 14.27% | 14.27% | **+4.76%** | 0.15 | 1.19× |
| Top 13 + shrunk-beta screen, 63d | 9.46% | 6.47% | −3.04% | — | 4.79× |
| Top 13 + **raw**-beta screen, 63d | 1.91% | 1.58% | **−7.93%** | — | 5.27× |
| *Control — equal-weight whole universe, 252d* | *15.28%* | *15.28%* | *+5.77%* | *1.04* | *1.19×* |
| *Control — bottom 13 by composite, 252d* | *46.08%* | *46.08%* | *+36.57%* | *2.58* | *1.08×* |

**The no-selection control beats the selection.** Equal-weighting the entire universe
returned +15.28% after tax against top-13's +14.27%. The ranking contributes nothing,
consistent with `agent_scorecards.json` reporting `quant.composite_score@21d` IC −0.201.

The bottom-13 control is a **diagnostic, not a strategy** — beta 2.58, vol 39.5%,
drawdown −25%, in a rising market, on a survivorship-biased universe that has already
deleted the high-beta names that went to zero. It confirms the composite is beta-sorting
and says nothing about expected return.

### The trap: do not screen per-name on the beta we compute

The raw 63-session beta band was the **worst arm in the study**. Blume shrinkage recovers
most of the damage (−3.04% at 63d, +1.41% at 252d) but still loses to not screening at
all. **Beta belongs at the portfolio level as a soft band on a long-window shrunk
estimate — never as a per-name gate.**

---

## 3. Architectural rules (non-negotiable)

1. **One portfolio, two rule sets — not two portfolios.** The output stays a single list
   of `{ticker, action, target_weight}` decisions flowing through the existing guard
   chain, `_compute_qty`, `pending_decisions.json`, the claim/stamp protocol, and
   `mark_transactions_live`. Nothing about order placement or idempotency changes.
2. **Ticker disjointness.** A ticker belongs to exactly one layer at a time, so a broker
   position maps 1:1 to a layer and no per-lot layer attribution is needed. The rejected
   alternative — lot-level tagging — creates a fourth log that can drift from the broker.
3. **Guards never invent orders.** All eight existing guards are strictly subtractive;
   that is what makes the chain safe to reason about. The beta band's **upper** bound is
   a guard (clamps); its **lower** bound is a **producer** upstream of the chain. A guard
   enforcing a floor would invert the failure direction from missed trades to unintended
   ones. (Now documented in `.claude/skills/senior_backend_engineer/SKILL.md`.)

---

## 4. Blockers

| # | Issue | Status |
|---|---|---|
| **B1** | `ROUTINE_DAILY_CYCLE.md` line ~304 computes an **absolute** share count on a stale-price re-quote while `execute._compute_qty` returns a **delta**. P1 today; becomes **P0** once the core producer makes add-to-holding BUYs routine. Requires a **live-routine sync**. | 🔴 OPEN — MANUAL_TODO **#22** |
| **B2** | ~~Fundamental coverage 72.9% vs the 80% floor~~ — **DIAGNOSED 2026-08-22: not a coverage failure.** Core-universe coverage is **96.0%**; the 72.9% is a blended reading over a rotating expansion batch, produced by a universe-gate oscillation (`_prior_coverage_ok` reads the previous *run*, not the previous *day*, across 4 daily crons). **No longer blocks Phase 2** — the re-weight is measured on the core universe. Two real defects remain to fix (gate hysteresis + coverage denominator), plus a **non-determinism risk on the trading path**: the routine's candidate set is 102 or 174 names depending on cron jitter, with a ~10-minute margin. | 🟡 OPEN (downgraded) — MANUAL_TODO **#24** |
| **B3** | Two date-pinned tests (`TestHistoryStoreFreshnessRecheck`) had silently stopped exercising the branch under test. | ✅ FIXED 2026-08-22 (PR #37) |

---

## 5. Phases

Risk class = what changes if the phase is wrong. **none** → no decision or order can
change · **selection** → which stocks get bought changes · **allocation** → how much
capital is deployed changes.

### Phase 1 — a beta estimate worth using · risk: none · ✅ SHIPPED (PR #37)

`quant_engine.beta_stable` — 252-session (history-capped) beta with Blume shrinkage
(`0.67·β + 0.33`) over **date-joined** returns, plus `beta_stable_raw`,
`beta_stable_window`, `beta_stable_basis` and `beta_stable_available`. The legacy
63-session `beta` is untouched (it is stamped into `factor_history.jsonl` and rendered
into the CRO risk block).

`beta_stable_available` means **"long basis"**, never merely "computed" — a 22–119
session estimate is exposed as `beta_stable_basis: "short"` for opt-in degradation but
does not satisfy the flag. Live: **72 of 174 names carry a short basis; 102 are sizable**,
38 of them in a 0.5–0.95 band.

Zero behavior change verified on live data — **0 differences across 174 tickers × 14
fields**. `FORMULA_VERSION` deliberately unchanged; no evidence clock reset.
`beta_stable` joins `_FACTOR_HISTORY_FIELDS` so the series accumulates from now.

### Phase 2 — neutralize the beta channel · risk: selection · ⚠ BUILT, HELD — must ship with Phase 3

Cross-sectionally regress `composite_score` on `beta_stable` each run and keep the residual.
Strips exactly the beta channel without re-deriving any sub-score formula.
`FORMULA_VERSION` → `3.0-beta-neutral`. Built on `feat/phase2-beta-neutral` (commit
`71ffb45`), 931 tests green, **not merged**.

**The mechanism works.** Acceptance gate passed with room, on the live snapshot:

| | Raw (2.3) | Neutralized (3.0) |
|---|---|---|
| ρ(composite, `beta_stable`) | −0.647 | **+0.000** |
| Spearman | −0.554 | +0.032 |
| ρ vs legacy 63-session beta | −0.591 | +0.006 |
| Mean-beta spread across quintiles | 1.18 | **0.19** |

**But it must not ship alone.** Across four arms on one consistent 205-session snapshot,
neutralization raises raw return almost entirely by raising **beta**, not by adding skill:

| Arm | Return | Realized beta | Jensen α | Δα |
|---|---|---|---|---|
| top13 eqwt 252d | 14.27 → **24.57** | 0.15 → **1.48** | +12.84 → +10.50 | **−2.34** |
| top10 eqwt 252d | 14.24 → **26.45** | −0.08 → **1.49** | +15.00 → +12.28 | **−2.72** |
| top13 eqwt 63d | 10.83 → 8.74 | 0.14 → 1.21 | +9.50 → −2.77 | **−12.27** |
| live shape 21d | 4.00 → 6.62 | −0.11 → 0.73 | +5.05 → −0.32 | **−5.37** |

Beta-adjusted alpha **falls in every arm**. Return/vol falls in 3 of 4 (1.57 → 1.20 on the
headline arm) and max drawdown roughly **doubles** (−5.01% → −11.24%). SPY rose 9.51% over
the window, so a beta-1.5 book earns ~14.3% from beta alone before any stock selection.

**Why it overshoots — the subtle part.** *Zero correlation is not a beta-controlled
portfolio.* Neutralizing makes the ranking orthogonal to beta **across the whole universe**,
but places no constraint on the **tail** — and the selection *is* a tail. Verified live:
top-13 mean `beta_stable` moves 0.26 → **1.13** (universe mean 0.82), and realized portfolio
beta lands at **1.48**. Nothing in Phase 2 targets a *level*.

**Consequence for sequencing:** Phase 2 alone swaps an unintended **−0.14** beta for an
unintended **+1.48** — both outside the owner's chosen 0.6–0.8 band — and spends the
`FORMULA_VERSION` reset measuring a book that is still not at target. **Phases 2 and 3 now
ship together as one unit and are evaluated as a pair.** The band in Phase 3 is what turns
"beta is no longer accidental" into "beta is 0.6–0.8 on purpose."

**Harness sanity check:** the no-selection control (equal-weight the whole universe) is
byte-identical under both modes (+0.00pp), as it must be since it never reads the score.

**Honest scope, unchanged:** this removes an unintentional bet; it does not add alpha. The
composite's measured IC is −0.201 and insignificant.

**Regime caveat:** one window, and a *rising* one. A low-beta book beats a high-beta book in
a flat or falling market — the raw arms' apparent advantage here is the mirror image of the
neutralized arms' apparent advantage. Neither is established across regimes.

### Phase 3 — the deterministic core producer · risk: selection

New `core_builder.py`: a **producer**, not a guard. Proposes 13 names at equal weight
≈7.7%, drawn from the neutralized ranking, subject to projected portfolio beta **0.6–0.8**
on `beta_stable` **with cash at beta 0**. Emits ordinary decisions carrying `layer: "core"`
and runs before the LLM sleeve.

**13 names at 7.7% is already legal** under the existing 8–15 holdings and 10%
max-position mandate. The 18.1% idle cash is a choice, not a constraint.

Because cash carries beta 0, a beta **floor is arithmetically a cash ceiling** — the
control that 29 consecutive DEGRADED `cash_discipline` runs never delivered
(`main.cash_discipline_status` is observability only and says so in its docstring).

Core names are exempt from rank-drift selling. They sell only on the −25% risk stop,
delisting, annual reconstitution, or a tax-loss harvest.

### Phase 4 — the beta band guard · risk: selection

`guardrails.enforce_beta_band(decisions, portfolio, betas, prices, lo, hi)`, modelled on
`enforce_sector_limits` (which already does post-trade weight projection and
clamp-with-qty-recompute). Returns `(kept, rejected, clamped)`; clamps land in
`validation_report["modified"]` → health stays DEGRADED.

**Ceiling only.** The floor is Phase 3's job — see §3 rule 3.

Wire after `enforce_sector_limits`, before `enforce_net_edge`. Must recompute `qty` at
the clamped weight (the sector-clamp bug class: a clamped weight with the oversized qty
breaches the cap *at the broker*).

### Phase 5 — measure the sleeve before throttling it · risk: measurement only

Realized **after-tax** return of the sleeve versus the core, attributed by the `layer`
tag, on a trailing window; surfaced weekly in `pipeline_digest` beside the Stage C
readiness line.

This number does not currently exist anywhere. Every other artifact measures forecast
accuracy; none measures whether the LLM's slice out-earned the deterministic slice after
tax. **Build the measurement before anything that acts on it.**

### Phase 6 — the sleeve budget controller · risk: allocation

Sleeve weight starts at a **20% floor**, capped at **40%**. Adjustment gated on evidence
sufficiency using the existing `stage_c_readiness` pattern (`n_effective` +
`ci_halfwidth` thresholds), moving at most ±5pp of book per quarter inside a wide
dead-band.

**Expect it to sit at the floor for roughly a year, and expect that to be correct.**
Driving a controller off a metric at `p ≈ 0.95` is a random number generator with a
governance story attached; the gate is what makes it a control system.

The rule is **pre-registered before it can act** (`PREREGISTRATION.md`). Post-hoc tuning
of this rule is the easiest available way to fabricate a result.

### Phase 7 — migrate without paying for it · risk: allocation

From {9 names, 18.1% cash, beta −0.14} to {13 names, near-fully invested, beta 0.6–0.8}.
Rebuilding in one Wednesday would trip the 50%-turnover circuit breaker at
`main.py:821` — and should.

Staged, tax-positive at every step:

- **Stage 0** — deploy the $95.32 cash into core names. No SELLs, no realized tax. Moves
  beta materially on its own.
- **Stage 1** — harvest losers: PLD −$1.68, CB −$2.04, AXP −$0.69 → **−$4.41** of
  realized losses that offset future gains and free capital. Tax-*positive*.
- **Stage 2** — hold winners (VRTX +$8.21, ABNB +$10.12, CVX +$2.99) past their one-year
  dates in mid-2027. `enforce_tax_aware_hold` already does this.
- **Stage 3** — the remainder converges at the annual reconstitution.

Liquidating to rebuild would realize ≈ **+$22** of net short-term gain at 54% ≈ **$12**,
or **2.3% of the book**, spent purely on reorganizing it.

---

## 6. Sequencing constraints

1. **Nothing that alters the decision path ships before the Wed 2026-08-26 rebalance.**
   `last_rebalance.json` shows Aug 19 executed with `"tickers": []`; the sector clamp
   (PR #36, merged 2026-08-20) has **never fired live**. Aug 26 is its first real test and
   must not be confounded. Phase 1 was safe on this basis — it changes no decision.
2. ~~**B2 before Phase 2**~~ — **lifted 2026-08-22.** The floor breach was a measurement artifact, not a data failure; core coverage is 96.0% and Phase 2 is measured there. B2's two real defects are independent of Phase 2 and can land after Wednesday.
3. **B1 before Phase 3** — the core producer makes add-to-holding BUYs routine, promoting
   the re-quote bug from P1 to P0. Needs a routines-UI sync, so it has owner lead time.
4. **Phase 5 before Phase 6** — never ship a controller before its input.
5. **Phase 2 and Phase 3 ship together** — added 2026-08-22 after the Phase 2 harness run.
   Phase 2 alone leaves portfolio beta uncontrolled at ~1.48; Phase 3's band is what sets the
   level. Shipping Phase 2 by itself would burn the evidence-clock reset on a book that is
   still not at the target exposure.

---

## 7. `FORMULA_VERSION` + the evidence clock

`FORMULA_VERSION` has moved four times (`2.0-quality-tilt` → `2.1-valuation-live` →
`2.2-valuation-sec-only` → `2.3-valuation-ttm`). Each was a genuine signal change and
each correctly reset the clock (IPS §3.3, §18.4). The consequence: **the primary metric
has never matured.** `agent_scorecards.json` still reports
`quant.composite_score@21d` unscored under the current formula, and `stage_c_readiness`
has read ACCUMULATING since it was built.

The strategy is changing faster than evidence about it can accrue. Phase 2 is a fifth
reset. It is justified — you cannot measure a beta sorter and learn anything about stock
selection — but **it must be the last for twelve months.** A reset with no freeze
commitment is a promise never to learn anything.

---

## 8. Governance obligations (IPS §18.4, DEPLOYMENT §7.0)

| Artifact | Change | Phase |
|---|---|---|
| `policy.yaml` | `policy_version` → 3.0; `beta_band_lo/hi`, `core_holdings_n`, `sleeve_weight_floor/cap` | 3, 4, 6 |
| `policy.py` `_DEFAULTS` | lockstep — `TestPolicyParity` asserts identity | 3, 4, 6 |
| `IPS.md` | Appendix A table; §6 holdings/cash; new §7 layer-discipline clause; Appendix B bump | 3, 6 |
| `PREREGISTRATION.md` | the sleeve controller rule, before it can act | 6 |
| `MODEL_REGISTER.md` | formula 3.0 entry with the clock reset dated | 2 |
| `RELEASE_NOTES.md` | `[Unreleased]` per phase (`TestReleaseNotes` enforces the section) | every |
| `DEPLOYMENT.md` | Phase 7 migration runbook; new Scenario F (core-integrity breach) | 7 |

`/code-review high` for Phases 1, 2, 5; **`/code-review ultra`** for 3, 4, 6, 7 (decision
or allocation path). Full `pytest` + ruff F821/F823. No `DRY_RUN main.py` on a trading
day — it overwrites `pending_decisions.json` (DEPLOYMENT §7.1).

**Routine sync:** Phases 1–6 require none — the routine reads `decision["qty"]` and
`target_weight` generically and never learns what a layer is. Only B1 touches a prompt.

---

## 9. Honest limits

**This removes a drag; it does not create an edge.** Nothing here makes the ranking
predictive. Expect the book to start tracking the market, not to start beating it.

**The book is currently winning because of the flaw.** Since inception: portfolio
**+5.31%** vs S&P **+4.07%** vs Nasdaq 100 **+0.29%** (dashboard, Aug 19). A near-zero-beta
book outperforms in a choppy market — QQQ swung to −7% in late July and clawed back while
the portfolio tracked a smooth line. The same construction lost **5.51pp** after tax
across the 205-session window where SPY rose 9.51%. **Moving to beta 0.6–0.8 means the
next weak market hurts more than the last one did.** That is the chosen trade; the first
drawdown will not feel like progress.

**The evidence base is one window on survivors.** Mid-analysis the committed snapshot
refreshed and one arm moved **6 percentage points** on a single extra day of data. Treat
arm *ordering* as informative and *levels* as directional only.

**Only one finding stands on its own:** the cross-sectional beta correlation
(ρ = −0.653, n = 100) — because it is a property of the scoring code, verifiable by
reading it, not a claim about returns needing another year to confirm. **The plan is
deliberately built on that one.**

---

## 10. Files touched (estimate)

| Phase | Files |
|---|---|
| 1 ✅ | `quant_engine.py`, `test_pipeline.py`, `RELEASE_NOTES.md` |
| 2 | `quant_engine.py`, `MODEL_REGISTER.md`, `PREREGISTRATION.md` |
| 3 | `core_builder.py` *(new)*, `main.py`, `policy.yaml`, `policy.py`, `IPS.md` |
| 4 | `guardrails.py`, `main.py`, `policy.yaml`, `policy.py` |
| 5 | `calibration.py`, `performance.py`, `pipeline_digest.py` |
| 6 | `main.py`, `policy.yaml`, `policy.py`, `PREREGISTRATION.md` |
| 7 | `core_builder.py`, `guardrails.py`, `DEPLOYMENT.md` |
