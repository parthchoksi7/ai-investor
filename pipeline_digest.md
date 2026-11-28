# Pipeline-Integrity Digest — week ending 2026-11-28

_Window: 2026-11-22 .. 2026-11-28 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 5
- **Fundamental coverage:** last 98.8% (min 97.2%, max 98.8%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 5}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## 🚨 Weekly rebalance (2026-W48): NONE recorded (last: 2026-W45 on 2026-11-04) — the heartbeat's Friday check should have alerted

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 26.6% above the 10% band · 33 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.129 ci=±0.215 n_eff=83
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.014 ci=±0.052 n_eff=1442
  - ✅ `event_present.flag@21d` — ic=-0.04 ci=±0.052 n_eff=1442

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 3 non-overlapping period(s), mean IC -0.042, 95% CI [-0.1898, 0.1058], n_obs 294 → **NOT_YET** · sector-neutral mean IC -0.0572
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.02855, q_bh 0.6335 (family of 15) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.0066, q_bh 0.9743 (family of 15) → NOT_SIGNIFICANT

_Generated 2026-11-28T04:01:11_
