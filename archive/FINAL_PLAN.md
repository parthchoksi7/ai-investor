# Final Plan + Expert Pre-Mortem

> **⚠ ARCHIVED (2026-07-05).** This Jun-13 roadmap (P0–P6) predates and is superseded
> by [`../STRATEGY_REDESIGN_PLAN.md`](../STRATEGY_REDESIGN_PLAN.md) +
> [`../IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md) (2026-06-27), which is what
> actually got built (Phases 0–5, live since 2026-07-04). Kept for historical record
> of the tax-recalibration reasoning and pre-mortem; not a current source of truth.
> For live status see `../CLAUDE.md` (Changelog) and `../MANUAL_TODO.md`.

Consolidated, decision-ready plan for the six edge-upgrade improvements, the tax
recalibration for a **California top-bracket taxable account**, and a full
Shreyas-Doshi-style pre-mortem by the twelve-expert panel.

Per-improvement expert designs live in [SOLUTION_PLANS.md](SOLUTION_PLANS.md);
this doc is the executive layer on top of it.

---

## Part 0 — What already shipped (foundation step done today)

**Paper-shadow 100× columns on `trades.csv`.** Every trade now carries a parallel
"what if the book were $50,000" view: `qty_100x`, `total_value_100x`,
`portfolio_value_100x` (same price, same date; qty and dollar value × 100). The
existing 16 rows were backfilled from their base columns; new rows and broker
reconciliation keep the twin in sync. 209 tests green (4 new).

- Code: `execute.py` (`SHADOW_MULTIPLIER`, `_scaled`, schema + write + migration
  backfill), `journal._reconcile_trade_log` (keeps `total_value_100x` consistent
  with the real fill price).
- **Known limitation (important — see pre-mortem #OPS-1):** these columns are a
  *linear projection*, not a real paper account. They assume zero market impact,
  perfect fractional fills, and identical prices at 100× size. For the mega-cap
  universe that's mostly fine, but it is **not** proof the strategy works at
  scale — it's a convenience lens. The real statistical-power fix is a true
  paper-shadow account with its own fills (Phase P4 below).

---

## Part 1 — The California tax recalibration (changes the whole thesis)

You are in the **top California bracket**, taxable account. Concrete marginal
rates on trading gains:

| Gain type | Federal | NIIT | CA | **Combined marginal** | You keep |
|-----------|--------:|-----:|----:|----------------------:|---------:|
| **Short-term** (held ≤ 1 yr; taxed as ordinary income) | 37% | 3.8% | 13.3% | **≈ 54%** | ~46¢/$ |
| **Long-term** (held > 1 yr) | 20% | 3.8% | 13.3% | **≈ 37%** | ~63¢/$ |

CA gives **no** preferential capital-gains rate — everything is ordinary income
at the state level. Implications that reshape the plan:

1. **The current weekly-churn momentum rotation is a tax wrecking-ball.** Every
   winning swap realizes a **short-term** gain taxed at ~54%. The strategy must
   clear a *much* higher pre-tax hurdle than SPY-buy-and-hold (which defers tax
   indefinitely and converts to long-term). A 2% pre-tax momentum edge is ~0.9%
   after-tax — before spread and before the opportunity cost of resetting the
   holding clock.
2. **Holding-period management becomes a first-class signal**, not a
   nice-to-have. Selling a winner at month 11 for a marginal momentum reason
   forfeits the ~17pp short→long rate improvement that was three weeks away.
3. **Tax-loss harvesting is partly blocked by your own churn.** The wash-sale
   rule disallows a harvested loss if the name is rebought within 30 days — and
   re-entry churn is exactly what the system does (the `recently_exited` guard
   exists precisely because of this pattern).
4. **#6 is promoted from "nice gate" to the single highest-EV change** for *your*
   tax situation, and **after-tax return becomes the primary backtest metric**,
   not a footnote.

This does not just tune a parameter — it argues the strategy's whole turnover
profile is wrong for the account it runs in. The plan below reflects that.

---

## Part 2 — The final roadmap (ordered, with exit criteria)

Build order is dependency-driven. Each phase has a hard **exit criterion** — do
not advance until it's met. "Forward-test, don't backtest, the LLM" is the spine
(a frozen model knows the future of any historical period).

| Phase | What | Why first | Exit criterion |
|------:|------|-----------|----------------|
| **P0 ✅** | Paper-shadow 100× columns | done | shipped, 209 tests green |
| **P0.5 ⭐** | **(a) Honest after-tax scorecard** — net return vs "hold SPY in this account," after CA tax + costs, on the existing track record. **(b) Cut turnover now** — raise the sell bar, holding-period bias, widen the wash-sale-aware re-entry block. | **Promoted by the pre-mortem.** These attack the two top failure modes (F1 death-by-taxes, F2 measuring the wrong thing) directly, need **no ML and no backtest**, and are each ~a sitting. Cutting weekly churn on a CA-top-bracket taxable book is *arithmetic*, not a hypothesis. | The dashboard shows the after-tax-vs-SPY number (labeled "n too small — not significant"); turnover/holding-period discipline is live in `guardrails.py` |
| **P1** | **#3 quant-only backtest + shared `cost_model.py`** (with the CA tax schedule baked in) | Nothing else can be validated without it; cost/tax model is shared with #6 | `python -m backtest` produces an **after-CA-tax** report vs SPY; CI smoke test gates regressions |
| **P2** | **#1 real data feed** (earnings calendar + estimates + fundamentals) | Biggest quality jump; quant quality/valuation go live *for free*; kills hallucinated catalysts; enriches the PIT archive P1 reads | No agent emits an earnings date contradicting the calendar; composite shows real quality/valuation; `fact_corrected` tracked in health |
| **P3** | **#2 prediction ledger + calibration** (score the full candidate universe, not the trade set) | The learning spine; produces labels P4/P5/P6 need | `forecasts_scored.jsonl` grows daily; per-agent IC/Brier with CIs; PM prompt carries real hit rates (gated behind n ≥ 200) |
| **P4** | **True paper-shadow account at ~100×** (real fills, not the linear column) + **#5 risk-based sizing** | The only way to get statistical power on a $500 live book; sizing validated in P1 backtest | Paper account runs the identical pipeline daily with its own fills; realized paper vol tracks target; backtest Sharpe ≥ LLM-weight baseline |
| **P5** | **#6 tax/cost gate + lot accounting** (CA short/long-term rates) | Shares P1 cost model; directly attacks the dominant drag for your account | Sub-net-edge trades skipped; holding-period guard active; measured turnover drops; after-tax backtest improves |
| **P6** | **#9 model-disagreement signal** | Cheapest; gated behind P3 validation | `disagreement_score` logged; confirmed negative IC before it haircuts any size |

**Sequencing rule:** P1 → P2 → P3 are the foundation and must land in order.
P4/P5/P6 can overlap once P3 is producing labels, but **none** may influence live
sizing until its own exit criterion (validation) is met.

---

## Part 3 — Expert Pre-Mortem (Shreyas Doshi method)

> **Frame:** It is **December 2026**. We shipped all six improvements. The project
> **failed** — the system was quietly shut off after underperforming a one-line
> "buy SPY and hold" alternative, net of tax, with more operational pain. Each
> expert has already seen the failure and explains, in detail, what killed it.
> Then we cluster, score (severity × likelihood), and fold the mitigation back
> into the plan. (Doshi's point: the most dangerous risks are the ones you're not
> tracking, and the most common failure is *fooling yourself with your own
> measurements*.)

### The post-mortem testimony

**Shreyas Doshi (facilitator, product):** "Three failures hide inside every dead
project like this: we built it *right* but it was the *wrong thing*; we built the
*wrong thing right*; or — the killer here — **we fooled ourselves into thinking
it worked when it never did.** I'll bet the house this one died of category
three. Also: the founder was the single biggest risk and nobody put a control on
him."

**Dana Okafor (tax-aware investing):** "It died on April 15th. The dashboard
showed +11% for the year. The 1099-B showed almost all of it as **short-term**
gains taxed at ~54% in California — and a pile of **wash-sale-disallowed** losses
because the re-entry churn rebought names inside 30 days. Net-of-tax, the book
trailed just holding SPY in the same account, which would have deferred every
dollar of tax. We measured and celebrated **pre-tax** returns for six months. The
one number that mattered for *this* account was the one we didn't put on the
chart."

**Dr. Marco Reyes (backtesting):** "Everyone trusted the full-pipeline backtest. I
warned that a 2026-cutoff model 'knows' how 2023 played out — the LLM layer's
backtest was contaminated look-ahead. We shipped a strategy whose backtested
'edge' was the model remembering that NVDA mooned. Live, with no future
knowledge, the edge wasn't there. The quant-only backtest was honest; the
moment we let the LLM into the backtest, we lied to ourselves."

**Tom Becker (execution/TCA):** "The 100× shadow columns were a linear
projection — same price at 100× size, zero market impact. Leadership pointed at
the green `total_value_100x` and said 'see, at $50k it works.' It was arithmetic,
not a fill. When the *real* paper account ran, fractional-share fills, spread,
and the timing slippage between the 9:45 signal and the actual fill ate the thin
edge. We confused a spreadsheet multiply for evidence."

**Dr. Hannah Wu (calibration):** "We turned on agent weighting too early. Even
with shrinkage, a few dozen *trades* isn't enough — and the team quietly scored
the *trade set* instead of the full candidate universe because the universe
join was fiddly. So we weighted agents on noise, the weights flipped sign every
month, and the PM chased its own tail."

**Sofia Almeida (signal combination):** "The forward-return labels were corrupted.
Splits and special dividends weren't adjusted in the snapshot archive, so a 10:1
split read as a -90% 'outcome.' Our beautiful IC numbers were measuring corporate
actions, not skill. Garbage labels in, confident garbage out."

**Lena Petrov (market-data platform):** "The cheap data vendor we picked had
earnings dates that were *estimated*, not confirmed, and drifted. We replaced
'the LLM hallucinates a date' with 'the vendor gives us a wrong date with a
confidence stamp.' Worse, because it now looked authoritative, nobody
double-checked it."

**Dr. Rajiv Menon (point-in-time data):** "The fundamentals feed was
as-reported-today, not point-in-time. The backtest used restated financials that
didn't exist on the trade date. Every value/quality signal in the backtest was
look-ahead. The live system and the backtest were quietly running on different
data."

**Dr. Yusuf Karim (portfolio construction):** "Vol-targeting on an 8-name book
with a 120-day covariance is pro-cyclical suicide. Vol spiked in the September
drawdown, the sizer de-risked at the bottom, then re-levered into the bounce —
we systematically sold low and bought high at the *portfolio* level, on top of
the momentum strategy already doing it at the *name* level."

**Elena Vasquez (bet sizing):** "We over-trusted the covariance for per-name
optimization despite agreeing not to. n=8 estimation error meant the 'optimal'
weights were random. Fractional Kelly on calibrated conviction would have been
robust; instead we shipped a precise-looking optimizer fit to noise."

**Dr. Priya Nair (uncertainty):** "Disagreement got wired into sizing before we
proved it predicted anything. It turned out high model-disagreement had *zero*
IC to outcomes for this universe — it was just the cheaper model being terse. We
haircut good trades for no reason and added latency."

**Aaron Stein (LLM eval):** "We measured disagreement as string-diff on the JSON,
so trivial wording differences read as 'high disagreement' and identical
decisions read as 'conflict.' The signal was an artifact of formatting. And the
ensemble tripled token cost on a system that was already marginal on economics."

**Dr. Marco Reyes (closing):** "And underneath all of it: at $500 live, and even
$50k paper, six months of data can't distinguish skill from luck at any
reasonable confidence. We were tuning a strategy inside its own noise band the
entire time and calling the wiggles 'learning.'"

### Clustered failure themes → severity × likelihood → mitigation

| # | Theme | Sev | Likely | Pre-emptive mitigation (folded into the plan) |
|---|-------|:---:|:------:|-----------------------------------------------|
| **F1 — Death by taxes** (pre-tax measured, ~54% ST drag, wash sales) | 🔴 High | 🔴 High | **After-CA-tax return is the headline metric from P1.** Ship P5 (lot accounting + holding-period guard + wash-sale-aware re-entry block) early, not last. Add an after-tax line to the public dashboard. The honest comparison is always "vs hold SPY in this account, after tax." |
| **F2 — Self-deception via measurement** (LLM look-ahead backtest; linear 100× shadow mistaken for proof; tuning inside noise) | 🔴 High | 🔴 High | **Backtest the deterministic layers only; forward-test the LLM.** Label the 100× columns as a projection in the UI. Require a pre-registered minimum sample / confidence before any "it works" claim. Quant-only and full-pipeline backtests reported side-by-side with the contamination caveat. |
| **F3 — Corrupted labels/data** (splits/dividends unadjusted; as-reported vs PIT; bad vendor earnings dates) | 🔴 High | 🟠 Med | Use adjusted closes everywhere; validate the snapshot archive for corporate actions (a -90% one-day "return" is a split, not an outcome). PIT-snapshot the feed daily (the git history) for backtests. Cross-check vendor earnings dates; carry a `confirmed?` flag and treat estimated dates as low-confidence. |
| **F4 — Calibration/disagreement turned on too early** (weighting noise; disagreement with no IC; string-diff metric) | 🟠 Med | 🔴 High | Hard n-gate (≥200 *universe* forecasts/agent) before weights move the PM. Score the **full candidate universe**, not the trade set. Disagreement measured **semantically on the structured decision**, logged-only until it shows negative IC. |
| **F5 — Sizing instability at n=8** (pro-cyclical vol-targeting; covariance overfit) | 🟠 Med | 🟠 Med | Shrunk covariance for the **portfolio vol scalar only**, never per-name optimization. Cap vol-target adjustment speed (no fast de-risk/re-lever). Inverse-vol base + fractional Kelly. Prove in P1 backtest before going live. |
| **F6 — The founder is the biggest risk** (overrides on noise, scope-creep, abandonment/bit-rot) | 🟠 Med | 🟠 Med | A personal "trading rule": no strategy changes off < N months of data; changes ship only when the backtest exit criterion is met. Keep scope to one phase at a time. A monthly checklist so the system doesn't rot. |
| **F7 — Ops/data-staleness path dependence** (cron flakiness, missed days, reconciliation gaps) | 🟡 Low | 🟠 Med | Already heavily mitigated (staggered crons, preflight gate, stamp-first idempotency). Add: alert if > 1 trading day missed; the paper-shadow account surfaces divergence between intended and filled. |

### The three risks we are *not* currently tracking (Doshi's real point)

1. **After-tax-after-cost net return vs "hold SPY in this account."** This is the
   *only* success metric that matters for a CA top-bracket taxable experiment,
   and nothing in the system reports it today. If we track one new number, track
   this one. (F1 + F2.)
2. **Whether any agent or signal has skill distinguishable from luck**, with a
   pre-registered confidence threshold. Without it, every up-month feels like
   validation and every down-month like noise — and we'll tune forever inside the
   band. (F2 + F4.)
3. **The gap between the linear 100× shadow and a real paper account.** The
   moment we treat the shadow columns as evidence rather than convenience, we've
   started lying to ourselves (F2/OPS-1). P4 closes this; until then the columns
   are labeled a projection.

---

## Part 4 — What changes in the plan because of the pre-mortem

1. **Promote the tax work.** After-CA-tax return becomes the P1 headline metric;
   lot accounting + holding-period guard + wash-sale-aware re-entry (P5) move up
   in priority. Consider the more radical option the tax math implies: **cut
   turnover hard and bias toward holding winners past 1 year** — possibly the
   highest-EV strategy change of all, independent of the AI.
2. **Never let the LLM into a backtest as if it were clean.** Quant-only backtest
   is the gate; the LLM layer is forward-tested only. State it in the report.
3. **Label the 100× shadow as a projection** wherever it's shown, and stand up a
   real paper account (P4) before any "works at scale" claim.
4. **Gate every learned signal** (calibration weights, disagreement haircut)
   behind a pre-registered sample/IC threshold; default to logging-only.
5. **Adopt a founder rule:** no strategy change off noise; one phase at a time;
   the exit criteria are the contract.

> **Bottom line from the panel:** the engineering will almost certainly succeed;
> the project dies — if it dies — from **measuring the wrong thing (pre-tax,
> look-ahead-contaminated, linearly-projected) and believing it.** Build the
> honest after-tax forward-test first, and most of the other failure modes lose
> their teeth.
