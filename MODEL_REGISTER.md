# Model Register — AI Investor (Model Risk Management)

> The SR 11-7-style **model inventory** for AI Investor. Every component that turns data
> into an estimate or decision is a *model* and is governed here: purpose, inputs,
> owner (defense line), risk tier, validation status, monitored KPI, and a
> **decommission criterion**. No model's output is treated as load-bearing until it
> clears the harness significance bar (§7.4 of the plan).
>
> **Linked artifacts:** [IPS.md](IPS.md) §10 (governance mandate) · [STRATEGY_REDESIGN_PLAN.md](STRATEGY_REDESIGN_PLAN.md) §18.3 (MRM design) · §7 (the harness that produces the KPIs).
>
> | | |
> |---|---|
> | **Version** | 1.0 |
> | **Effective** | 2026-06-27 |
> | **Review cadence** | Quarterly (with the investment review, plan §18.6) |
> | **Default status** | All models `NOT_VALIDATED` — none has cleared the bar yet. This is honest, not a defect. |

---

## 1. Risk tiering

| Tier | Definition | Validation rigor |
|------|-----------|------------------|
| **High** | Directly sizes or vetoes capital | Hardest bar; KPI monitored every harness run; decommission reviewed quarterly |
| **Medium** | Shapes the decision but cannot act alone | IC + counterfactual monitored; decommission reviewed semi-annually |
| **Low** | Enrichment / context only; cannot move capital | Sanity + parse-rate monitored only |

---

## 2. The register

| ID | Model | Line | Tier | Inputs | Output | Primary KPI | Validation status | **Decommission if…** |
|----|-------|------|------|--------|--------|-------------|-------------------|----------------------|
| **M1** | Regime Strategist (Agent 1) | 1st | High | snapshot, macro/news | Risk-on/neutral/off | Regime-conditioned return vs unconditional | `NOT_VALIDATED` | regime calls don't improve risk-adjusted return vs always-neutral after ≥ N obs |
| **M2** | Research Analyst (Agent 2) | 1st | Medium | dossier, history, prior outcomes | thesis + confidence | IC of `research.confidence` at 63/126/252d | `NOT_VALIDATED` | confidence IC not significantly > 0 at primary horizon AND removal doesn't cut after-tax return |
| **M3** | Earnings/Catalyst (Agent 3) | 1st | Medium | events, earnings calendar | catalyst alpha score | IC of `earnings_alpha_score` | `NOT_VALIDATED` | alpha score IC ≤ 0 after ≥ N matured obs |
| **M4** | Devil's Advocate (Agent 4) | 1st | Medium | dossier, thesis | bear case + reject flag | **Counterfactual:** do rejected names underperform held? | `NOT_VALIDATED` | rejected names do **not** underperform the held set (the DA adds no risk reduction) |
| **M5** | Position Review (Agent 5) | 1st | High | holding + entry anchor + since_entry | hold/reduce/exit | hit-rate of REDUCE/EXIT vs subsequent return | `NOT_VALIDATED` | exit calls don't beat hold-to-rebalance after tax |
| **M6** | Portfolio Manager (Agent 6) | 1st | **High** | dossier, bench, scores | target weights | after-tax book vs equal-weight & quant-only | `NOT_VALIDATED` | book doesn't beat equal-weight top-N after tax (no sizing skill) |
| **M7** | Chief Risk Officer (Agent 7) | 2nd | **High** | proposed trades, correlation, sector | veto / clamp | **Counterfactual:** do vetoed trades underperform? + breach-prevention rate | `NOT_VALIDATED` | vetoed names don't underperform AND no breaches prevented (veto is noise). NB: hard limits stay in `guardrails.py` regardless — see §2.1 |
| **M8** | Quant composite | 1st | **High** | OHLCV, fundamentals | factor scores + rank | IC of `composite_score`; the burden-of-proof baseline | `NOT_VALIDATED` | **Non-decommissionable baseline** — if its IC ≤ 0 the conclusion is *"go passive / hold SPY"* (IPS §3.5–3.6 Abandon), **not** "remove the floor." Judged, never removed |
| **M9** | Event digest (Haiku) | n/a | Low | raw news | structured events | parse-success rate ≥ 80% | monitored | parse-rate chronically < 80% or events add no dossier value |
| **M10** | Deep-dive analyst (§14) | 1st | Medium | dossier deltas (work queue) | bench conviction notes | do high-conviction bench names outperform at 63/126d? | `NOT_VALIDATED` | bench conviction has no forward predictive power |

### 2.1 Control independence note
M7 (CRO) is a 2nd-line *model* but is **not** the binding control — every hard limit in
[IPS.md](IPS.md) Appendix A is enforced deterministically in `guardrails.py`. The CRO's
qualitative veto is additive. This preserves 2nd-line independence even if the CRO model
is decommissioned (plan §18.2).

---

## 3. Validation lifecycle

```
NOT_VALIDATED ──(clears IC/counterfactual bar, harness)──▶ VALIDATED ──(load-bearing)
      │                                                          │
      │  (fails decommission criterion)                          │ (KPI degrades below floor)
      ▼                                                          ▼
  DECOMMISSIONED  ◀───────────────────────────────────────  ON_WATCH
```

- **NOT_VALIDATED:** output is logged, measured, **and used in the live book** (the
  capital is insignificant — [IPS.md](IPS.md) §3.4), but is **not relied upon for any
  capital-scaling decision** and is treated as *abstaining* in the scientific verdict,
  which is read off the paper arms. This is the current state of all decision models.
- **VALIDATED:** cleared the §7.4 significance bar at the primary horizon; may be relied on.
- **ON_WATCH:** a previously-validated model whose KPI dropped below floor; reverts to
  non-load-bearing pending review.
- **DECOMMISSIONED:** removed from the pipeline; the change is a §18.4 policy event and the
  performance record is segmented at the boundary.

---

## 4. Monitoring

Every harness run (plan §7) updates each model's KPI into `signal_scorecard.json`; the
quarterly review (plan §18.6) updates this register's **Validation status** column and
records any tier or status change in §5.

The honest power caveat (plan §7.4) applies: at ~4 forecasts/month, per-name LLM models
(M2–M5, M10) may take **1–2 years or never** to reach significance. Until then they stay
`NOT_VALIDATED` — the register shows the truth rather than pretending.

### 4.1 Model & prompt version governance — record + governed adoption, NOT freeze

Every model carries an `underlying_model_version` and a `prompt_version`, **recorded on
every decision it makes.** Pinning here means *record + govern*, **not** *freeze on an
old model* (freezing would forgo real improvements, and providers deprecate old models
anyway):

- **Record always** — so any decision is reconstructable and performance can be segmented
  at a version boundary. Pure upside, near-zero cost.
- **Adopt new models deliberately** — a new provider model or a prompt change is a
  **change-control event** (plan §18.4): run the candidate in **shadow against the
  pinned version on the same inputs** for a cycle (the shadow arm already exists); if it
  is non-regressive / better **on this system's task** (not merely on public
  benchmarks), promote it and **segment the performance record at the boundary**.
- **Why** — this captures genuine model improvement (a smarter LLM *should* be adopted)
  while preventing a silent swap from (a) **confounding the measurement** — fatal for a
  platform whose whole job is to measure if the LLM adds value — or (b) introducing a
  task-specific regression (e.g., a verbosity/format change that breaks the JSON parser,
  a failure this system has already hit). Note also that model IQ is **not** this
  system's bottleneck — data coverage and breadth are (plan §7.4 / §8) — so a model
  upgrade is not expected to be the primary performance lever.

---

## 5. Change log

| Version | Date | Model | Change | Rationale |
|---------|------|-------|--------|-----------|
| 1.0 | 2026-06-27 | all | Initial inventory; all `NOT_VALIDATED` | Establish the MRM baseline at adoption |
