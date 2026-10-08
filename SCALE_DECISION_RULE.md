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

Gate 1 has two halves (1a and 1b) and **both** are required; Gates 2–4 are single.

### Gate 1 · The predictions work — **and the AI is the reason** (the gate with real power)

**Amended 2026-08-27, before any result exists.** The original Gate 1 pointed at
`PREREGISTRATION.md`'s primary metric, which is `quant.composite_score` — the **deterministic**
formula, with no AI involved anywhere in it. As written, the rule could have been satisfied
by a system that performs identically with all seven agents switched off, while still paying
for them. That does not answer this account's question (CLAUDE.md → Purpose). Corrected here
while there are no results to be tempted by; changing it later would be moving the goalposts.

**Both halves are required.**

#### Gate 1a · The foundation works — the deterministic score predicts

`quant.composite_score@21d` reaches **either**:

| Route | IC | Independent observations | ≈ time |
|---|---|---|---|
| **Fast** | ≥ **0.08** | ≥ **400** | ~1.3 years |
| **Slow** | ≥ **0.05** | ≥ **1,600** | ~3.4 years |

with a confidence interval **excluding zero**, measured **entirely within one
`FORMULA_VERSION`** — no mixing across scoring changes.

#### Gate 1b · The AI adds something on top of it

At least one of the following, **BH-adjusted** (`significant_bh` in
`agent_scorecards.json`, `p_value_bh` in the counterfactual):

- **An agent's own signal predicts** — any of `research.confidence`,
  `devils_advocate.overall_risk_score`, `position_review.hold_score` clears the same IC and
  observation bar as Gate 1a, **in its expected direction**; or
- **The AI's decisions predict** — in `counterfactual.json`, `pm_selected` (the names the
  Portfolio Manager chose beat the ones it passed on) or `da_reject` (the names the Devil's
  Advocate flagged underperform) shows the correct sign with **both sides ≥ 10** and BH-adjusted
  **p < 0.05**.

**Multiple-comparison correction is mandatory, not optional.** Allowing "any one of five
signals" is five independent chances at a 5% false-positive rate — roughly a 1-in-4 chance of
passing on noise alone. Raw p-values do not satisfy this gate.

**Two signals are excluded as structurally unmeasurable**, and must never be used to satisfy
1b: `cro_veto` (n_flagged = 1–2 — the CRO rarely vetoes a *named* ticker, so it cannot reach
the n ≥ 10 floor) and `pm.expected_return` (n_effective = 7, because only BUYs carry the
field). Citing either would be passing the gate on a sample that cannot support it.

#### If 1a passes and 1b fails — that is a RESULT, not a failure

It means: **the deterministic factor model works and the AI is decoration.** That is a
genuinely valuable finding, arguably more actionable than the alternative, and the correct
response is to run the quant layer *without* the LLM pipeline and stop paying for it —
`deliberation_stats.json` notes per-agent cost is still unmeasured, which should be fixed
before that comparison is made.

Do **not** scale the agentic account in that case. Scale the deterministic strategy instead,
which is a different and much cheaper system.

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
| 0 · today | — | **$500** (actual balance; the previously-discussed increase to $1,000 is **not planned for now** — decided 2026-08-26) |
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

## 7. Decisions taken

### Bug fix that does not alter decisions — 2026-10-07: published benchmark closes are dated

**No threshold in this document changed, and nothing that decides a trade changed.**
Logged per §6 ("Bug fixes that do not alter decisions are allowed and must be dated in
this section").

**Fact for the record: the frozen window opened with rebalance `20261007-144809`,
executed 2026-10-07** (`last_rebalance.json`, ISO week 2026-W41). The freeze commitment
in the 2026-10-03 entry below runs from that rebalance.

**The fix.** `publish.py` wrote the prior session's SPY and QQQ closes onto the dashboard
row for each day (about 34 trading-day rows, 2026-08-07 through 2026-10-07; third
occurrence). A benchmark close now has to carry the date of the bar it came from; the
4 PM publish writes NULL rather than a prior-session close; every publish re-checks the
previous 14 weekday rows (as does one evening run, `benchmark_reconcile.yml`); and
`backfill_benchmark_closes.py` repairs the older rows once.

**Why it does not alter decisions.** The change is confined to `publish.py`'s Supabase
write, a new backfill script, tests and documents. It touches only four columns of the
dashboard table `portfolio_snapshots` (`spy_close`, `qqq_close` and their cumulative
returns). The trading routine runs with `DRY_RUN=true`, under which `publish_to_supabase`
returns before any of this code runs; the code executes only in GitHub Actions (the
publish job and the evening reconcile job), after the day's decisions are final. No prompt, universe, sizing, policy
limit or `FORMULA_VERSION` is touched.

**Why it does not touch the evidence.** Gate 1a (`quant_ic_report.json`), Gate 1b
(`counterfactual.json`, `agent_scorecards.json`) and Gate 2 (`performance.py`) are
computed from the repo's ledgers and the market snapshot's dated price history. None of
them reads these Supabase columns. Every gate's clock continues uninterrupted.

### Restart — 2026-10-03: the first window was contaminated; one restart, then a real freeze

**No threshold in this document changed.** What changed is the system being measured,
which §6 says restarts the window. This entry exists so the restart is on the record
with its reasons, *before* the next result.

**Contamination of the window that opened 2026-09-02** (logged per §6, independent of
any decision to change the system):

- **W39 (Sep 21–25) had no rebalance at all.** GitHub Actions was billing-blocked from
  Sep 21 evening to Sep 30, so no market snapshot landed for 7 trading days.
- **The alerting plane was dark for the same 8 days.** It shares the blocked billing
  account; no alert fired until 2026-10-01.
- **The −25% stop-loss check did not run on 5 trading days** (Sep 23–25, Sep 30–Oct 1).
  `preflight_gate.py` skipped catch-up days with stale data. No loss resulted.
- **The Oct 2 rotation half-executed.** Its SELLs filled; its three BUYs never reached
  the broker, because this cash account cannot spend same-day sale proceeds.
- **Throughout, the routine traded on different scores than this rule measures.** For
  62 short-history names, the routine re-scored a slimmed snapshot. Gate 1a reads
  `factor_history.jsonl`, which is computed on full history.

**What the restart changes** (`PLAN_OCT3_REMEDIATION.md`, policy v2.1, IPS v1.4):
- **Prompts:** the PM and CRO are told the benchmark and shown exposure/sector context.
- **Universe:** PARA, now a reused micro-cap symbol, is replaced by PSKY; a
  young/reused-ticker guard is added; and the expansion gate gets hysteresis.
- **Sizing:** BUYs are sized to settled cash.
- **Policy:** a 63-trading-day minimum hold for positions at a gain, and an inclusive
  wash-sale window.
- **Scoring source:** the routine now trades on the recorded full-history scores.

**What it does NOT change:** `FORMULA_VERSION` (`3.0-beta-neutral`). Gate 1a's
evidence clock therefore **continues uninterrupted**. Only Gate 1b (the AI) and Gate 2
(the account) restart.

**The honest tension, stated rather than buried.** This restart comes after a bad month.
That is exactly the pattern §5's kill rule guards against: "the scoring formula has to be
changed again to keep the result alive."

It does not trip that clause, for three reasons:
1. The formula is untouched.
2. Every change is grounded in a defect that would be true whatever the returns were:
   a missed week, a disabled stop, an unplaced order, a scoring mismatch, a reused
   ticker, and agents never told the benchmark.
3. The investigation that found them measured the return gap at ~0.6σ of this book's
   tracking error, which is not evidence of failure.

**The new window opens at the first rebalance executed after this batch is merged and the
live routine is synced (target Wed 2026-10-07).**

**Freeze commitment.** From that rebalance, prompts, universe, sizing, policy limits and
`FORMULA_VERSION` are frozen. Bug fixes that do not alter decisions are allowed and must
be dated in this section. Any other change restarts the window again **and counts
against §5's kill rule as a second restart.**

### Measurement amendment — 2026-10-04, before the frozen window opens (no threshold changed)

Owner-directed, from the 2026-10-04 end-to-end review (`PLAN_OCT4_REVIEW.md`). **Every
threshold in §3 is unchanged.** What changes is how two of them are *read*, because the
existing reads could not support the decision this rule makes:

- **Gate 1a is read from `quant_ic_report.json`.** IC = the mean of one cross-sectional
  Spearman IC per date over the **core universe** (`universe.CORE_UNIVERSE`, ~98 names
  scored every day — the full cross-section swings 100↔240 with the expansion gate, which
  would mix two populations into one mean), from `factor_history.jsonl`, using only
  non-overlapping 21-day periods (greedy from the first eligible date; the next period's
  signal date must be on/after the previous period's actual exit date). "Independent
  observations" = names summed over those periods (so the §3 floors of 400 / 1,600 keep
  their meaning). "CI excluding zero" = the Student-t 95% interval on the per-period ICs,
  which is **stricter** than the pooled read it replaces (a single month cannot count as
  hundreds of independent observations). The calibration ledger's candidate-only IC
  (`agent_scorecards.json`) continues to be reported as registered.
  Per-date ICs are written once to the append-only `quant_ic_history.jsonl` (GH plane
  only), so periods never drop out as the ~210-bar price window rolls and the greedy
  non-overlap anchor never moves.
- **Gate 1b is read from `counterfactual.json`**: each run date's mean return is removed
  before flagged and kept names are compared (Welch's **t**, not z), and ONE
  Benjamini-Hochberg correction covers the whole Gate 1b family at once — `pm_selected` and
  `da_reject` at every horizon plus the three agent signals' scorecard p-values (listed in
  `_gate_1b`). `p_value_bh` / `significant_bh` are those joint-family values. The field this
  rule always named did not exist until today.

**Disclosure — what was seen before this was written.** While building the full-universe
read on 2026-10-04 the reviewer looked at per-date ICs across *all* formula versions since
July (mean −0.09; under `3.0-beta-neutral`, 10 matured dates, one non-overlapping period at
−0.11) and at the counterfactual (PM selection, demeaned: −4.3 points at 21 days; Welch-t
p = 0.07, joint-family BH q = 0.54). None of these satisfies or comes near any gate; the
amendment is made because the old reads were mis-specified, not because of what they showed.

**Also shipped before the freeze, as part of the restart batch** (both change decisions, so
both ship before the first rebalance of the frozen window, Wed 2026-10-07, not during it):
the −25% stop now runs on the rebalance day (`main.inject_risk_stops`); and the bounded
mechanical cash deployment (`cash_deployment.py`, policy v2.2, IPS v1.5) — sizing, listed
in §6 as a window-restarting change, which is why it lands now and not later. Its top-ups
of existing holdings are excluded from `pm_selected`, so a deterministic rule can never
satisfy Gate 1b on the AI's behalf. With it, one PM prompt addition (a prompt change, same
reason to land now): above the cash band the PM is told the holdings arithmetic — the rule
adds no names, so only the AI's BUYs can bring cash into the band.

### Amendment — 2026-08-27, before any result existed

**Gate 1 split into 1a (deterministic foundation) + 1b (the AI adds something).** The
original pointed only at `quant.composite_score`, which contains no AI, so the rule could
have been satisfied by a system that works identically with all seven agents off. That
answers the wrong question for this account. Both halves are now required, 1b demands
BH-adjusted significance to stop "any one of five signals" manufacturing a false positive,
and two signals (`cro_veto`, `pm.expected_return`) are excluded as structurally
unmeasurable at n = 1–7.

Made before the measurement window opened (first scored rebalance 2026-09-02) and before any
qualifying result existed. Any *further* change to a threshold restarts the window.

### Original decisions (2026-08-26)

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

   **CONFIRMED 2026-08-26: SPY.** The owner confirmed SPY as the gate. QQQ continues to be
   reported alongside it, but does not gate the decision. This question is closed.
