# Pipeline-Integrity Digest — week ending 2026-11-21

_Window: 2026-11-15 .. 2026-11-21 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 4
- **Fundamental coverage:** last 97.8% (min 97.8%, max 98.8%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 4}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## 🚨 Weekly rebalance (2026-W47): NONE recorded (last: 2026-W45 on 2026-11-04) — the heartbeat's Friday check should have alerted

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 26.6% above the 10% band · 33 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.105 ci=±0.222 n_eff=78
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.016 ci=±0.053 n_eff=1371
  - ✅ `event_present.flag@21d` — ic=-0.046 ci=±0.053 n_eff=1371

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 3 non-overlapping period(s), mean IC -0.042, 95% CI [-0.1898, 0.1058], n_obs 294 → **NOT_YET** · sector-neutral mean IC -0.0572
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.02855, q_bh 0.617 (family of 15) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.0066, q_bh 0.9127 (family of 15) → NOT_SIGNIFICANT

_Generated 2026-11-21T03:17:23_
