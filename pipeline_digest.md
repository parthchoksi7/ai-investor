# Pipeline-Integrity Digest — week ending 2026-10-17

_Window: 2026-10-11 .. 2026-10-17 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 8
- **Fundamental coverage:** last 97.2% (min 93.9%, max 97.8%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 8}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## Weekly rebalance (2026-W42): ✅ executed on 2026-10-14 — 2 ticker(s) traded

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 28.6% above the 10% band · 30 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.075 ci=±0.364 n_eff=29
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.006 ci=±0.063 n_eff=964
  - ✅ `event_present.flag@21d` — ic=0.03 ci=±0.063 n_eff=964

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 1 non-overlapping period(s), mean IC -0.1085, 95% CI None, n_obs 98 → **NOT_YET** · sector-neutral mean IC -0.1561
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.04407, q_bh 0.4692 (family of 12) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.00526, q_bh 0.9122 (family of 12) → NOT_SIGNIFICANT

_Generated 2026-10-17T00:36:35_
