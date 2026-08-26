# Scale Decision Rule — when to move real money into the agentic account

**Status: ADOPTED 2026-08-26.** Thresholds below are now binding. Changing any of them
requires saying so explicitly and restarting the measurement window.

**Purpose.** The owner intends to transfer money from their Robinhood account into the
agentic account **if the experiment works**. This document defines what "works" means,
*in advance of the results*, so the decision is made on evidence rather than on a good run.

Written before any result qualifies. Once adopted, changing a threshold requires saying so
explicitly and restarting the measurement window — the same discipline `PREREGISTRATION.md`
already applies to the per-agent skill numbers.

---

## 1. Why a rule written in advance

Without one, exactly two things happen, and both are bad:

- **You scale after a lucky streak.** Three good months feel like proof. They are not.
  This is the ordinary way people lose money on systems that never worked.
- **You never scale**, because no amount of evidence ever feels like enough, and the
  experiment quietly becomes a hobby with no decision attached.

A written rule removes both. The right time to choose the bar is now, while there are no
results to be tempted by.

---

## 2. The hard constraint: what can and cannot be proven

A concentrated portfolio of ~13 stocks naturally drifts about **10% a year** away from the
index simply from being concentrated. An edge has to stand out from that noise.

| If the real edge is | Years of account history needed to prove it |
|---|---|
| 2% per year | ~100 years |
| 3% per year | ~44 years |
| 5% per year | ~16 years |
| 8% per year | ~6 years |
| 12% per year | ~3 years |
| 20% per year | ~1 year |

**The account's own return cannot settle this question in a human timeframe** unless the
edge is enormous. Any rule built only on "did it beat SPY" is a rule that will be satisfied
by luck long before it is satisfied by skill.

The system's *predictions* carry far more information. It scores ~174 stocks every week,
so it produces thousands of testable calls rather than a few dozen trades. The measure is
**IC** — how well the ranking predicts what actually happens next. 0 means useless; 0.05 is
a genuinely good professional quant signal.

| Signal strength (IC) | Independent observations needed | ≈ time at the current rate |
|---|---|---|
| 0.03 | 4,444 | ~9.4 years |
| 0.05 | 1,600 | ~3.4 years |
| **0.08** | **625** | **~1.3 years** |
| 0.10 | 400 | ~0.8 years |
| 0.15 | 178 | ~0.4 years |

**This forces a deliberate choice, and it should be made consciously:** the rule below
requires a *strong* signal, because a weak one cannot be told apart from zero before the
2030s. That means a real-but-modest edge would be missed. That is accepted on purpose —
the cost of missing a modest edge is small; the cost of scaling into a signal that was
never there is not.

---

## 3. The gates — all four must pass

### Gate 1 · The predictions work (the gate with real statistical power)

**Two routes, either one satisfies the gate.** A single threshold forced a bad choice: 0.08
is decidable in ~1.3 years but is better than most professional quant funds achieve, so it
might never be reached; 0.03 is realistic but takes ~9 years. Two routes remove the choice —
a strong signal earns a fast decision, a modest one still counts but must prove itself longer.

| Route | Signal (IC) | Independent observations | ≈ time |
|---|---|---|---|
| **Fast** | ≥ **0.08** | ≥ **400** | ~1.3 years |
| **Slow** | ≥ **0.05** | ≥ **1,600** | ~3.4 years |

Either route additionally requires:
- a confidence interval that **excludes zero**, and
- measurement **entirely within one `FORMULA_VERSION`** — no mixing across scoring changes.

*Source: `agent_scorecards.json`, the existing `stage_c_readiness.py` machinery.
Note this is a stricter bar than that file's current `MIN_N_EFFECTIVE = 30` /
`MAX_CI_HALFWIDTH = 0.15`, which was set to decide a build question, not a money question.*

### Gate 2 · The account doesn't contradict the predictions

- **After-tax** return beats SPY over **at least 12 months**, and
- portfolio market-exposure sits between **0.8 and 1.2** — so it is a fair comparison
  rather than a bet on taking less (or more) risk than the benchmark

This gate cannot *prove* skill — §2 shows why. It exists to **disprove**: if the
predictions look strong but the account still loses, the problem is in execution, sizing,
or costs, and scaling would carry that flaw with it.

### Gate 3 · It isn't one lucky stretch

- Split the measurement window in half. The edge must be **positive in both halves**.
- Remove the single best month. It must **still beat SPY after tax**.

*This exists because the ordering of results in this project has already flipped between
halves of a two-year window (`PLAN_BETA_ALPHA_SPLIT.md` §5b).*

### Gate 4 · It survives tax at the size you'd actually run

Every figure above is **after California taxes at the account's real turnover.**

This is not a formality. On the measured two-year test a strategy earned **+39.52% before
tax and kept +18.41%** — more than half the profit lost to trading tax. Scaled to $100,000
that is roughly **$21,000**. A design that only looks good before tax is not a result you
can use.

---

## 4. Staged scaling — never all at once

Passing the gates once buys a **first** transfer, not a full commitment.

Caps are expressed as a **share of net worth**, not fixed dollars, so they stay sensible as
net worth changes rather than quietly becoming too large or too small.

| Stage | Trigger | Maximum in the agentic account |
|---|---|---|
| 0 · today | — | **$500** (current actual balance; a planned increase to $1,000 has not been made) |
| 1 | All four gates pass | **1% of net worth** (~$8,000 today) |
| 2 | Gates still pass 12 months later, *at the larger size* | **5% of net worth** (~$40,000) |
| 3 | Gates still pass 12 further months | Owner's decision, fresh review |

Worst realistic case at stage 2: trailing the index by 10%/yr for two years on ~$40,000 is
about **$8,000** worse off than simply holding — roughly 1% of net worth. Bad, not damaging.

Re-checking at each size matters: an edge can be real at $1,000 and evaporate at $50,000
through costs, slippage, or simply because the earlier result was luck that hadn't yet
been revealed.

---

## 5. The kill rule — when to stop

Equally important, and usually missing. Stop the experiment and return the money if:

- **24 months** pass without Gate 1 reaching even **IC ≥ 0.05 at n_effective ≥ 400** —
  the signal is then too weak to ever be provable on a useful timescale; or
- after-tax return trails SPY by more than **15% cumulative** over any 12-month window
  while market exposure is inside the 0.8–1.2 band (i.e. losing on picks, not on risk); or
- the scoring formula has to be changed again to keep the result alive. A result that
  requires the measurement to keep moving is not a result.

---

## 6. What must stay frozen while the clock runs

The measurement is only valid if the thing being measured stops changing:

- **`FORMULA_VERSION` is frozen.** It has already been changed four times, and every change
  restarted the clock — which is precisely why nothing has ever concluded.
- Changes to prompts, universe, or sizing **restart the window**. Bug fixes that do not
  alter decisions do not.
- Every change is logged with its date so any window can be checked for contamination.

---

## 7. Decisions taken (2026-08-26)

1. **Signal threshold — two routes** (§3 Gate 1). Rationale: the risk is lopsided. A false
   positive moves real money into noise and costs years plus confidence; a false negative
   just means carrying on with what already works. The bar leans strict.
2. **Stage caps — 1% then 5% of net worth**, re-checked at each size. An edge can be real at
   $500 and vanish at $40,000; the re-check is the point of staging, not a formality.
3. **Kill horizon — 24 months**, plus a **12-month machinery check**. That check is *not* a
   chance to move thresholds — it verifies the plumbing still works (data flowing, forecasts
   scoring, nothing silently broken). This project has form: a coverage figure once looked
   like a crisis and was a broken measurement, and two safety checks passed for weeks
   without testing anything. Reaching month 24 to find the last year measured nothing would
   be the worst outcome available.
4. **Benchmark — SPY is the gate; QQQ is reported alongside.** QQQ is a bet on technology as
   much as a higher bar: requiring it would mean failing in a year tech runs hot despite
   genuine skill, and passing in a year tech falls without any. SPY is the honest "did
   picking stocks beat owning stocks" test.

   **Open, and able to override this:** the true benchmark is whatever the transferred money
   would otherwise have been invested in. If the Robinhood holdings earmarked for transfer
   are mostly QQQ, then QQQ is the real opportunity cost and becomes the gate. Owner to
   confirm; until confirmed, SPY stands.
