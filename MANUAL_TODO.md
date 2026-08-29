# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
A few items are Claude-buildable but deliberately parked on an owner decision; those say so.
Highest-priority concern first.

_Last refreshed: **2026-08-22** — full audit and cleanup. Every item below was re-verified
against a real artifact, workflow run, or API call in this session; 16 resolved items were
collapsed into the Closed archive at the bottom (git history preserves their full bodies).
Two were found **stale in the file itself**: `UNIVERSE_EXPANDED` was listed "pending — your
call" but has been **live since ~2026-08-13** (`market_snapshot.json` → `universe_expanded:
true`), and the go-live observation window (#13) closed seven weeks ago. One previously-open
item (#24) was diagnosed and downgraded — it is not the coverage failure it appeared to be._

**Status legend:** `[x]` = done and verified · `[ ]` = not yet done · the **DONE** /
**PARTIAL** / **PENDING** / **AWAITING DECISION** tag after each title gives the one-line
state without reading the body. "Verified" means checked against a real artifact, workflow
run, or API response in this repo — never assumed from memory.

---

## 📋 Open items

| # | Item | Status |
|---|------|--------|
| 22 | **Re-quote qty mismatch: absolute vs delta** (routine STEP 4 vs `_compute_qty`) | 🟠 **CODE FIXED 2026-08-28 — awaiting routine sync.** `ROUTINE_DAILY_CYCLE.md` corrected; the LIVE prompt still carries the bug until pasted. Two bugs, not one: over-buys an add-to-holding BUY, **and silently skips a `target_weight=0` full exit** |
| 25 | **Reset `portfolio_peak.json` if/when the account is ever funded** | ⏸️ **PARKED 2026-08-26** — no deposit planned for now. Re-arms the moment one is made |
| 24 | **Coverage "regression" — DIAGNOSED, not a data failure** | 🟡 **2 defects to fix** (gate hysteresis + coverage denominator). Does **not** block Phase 2. Do not ship before Wed 2026-08-26 |
| 11 | **`since_entry` dossier anchor is structurally always `None`** | 🟡 **PENDING** — re-verified 2026-08-22: 172 dossier records, **0** non-null. Inert since it shipped |
| 14 | **Narrow the risk_watch cross-mode interlock** | ⬜ **PENDING, not built** — key the interlock off rebalance SELLs only |
| 15 | **Crash-evidence preservation in risk_watch** | ⬜ **PENDING, not built** — archive a claimed-but-unstamped envelope before overwriting |
| 17 | **Prompt-drift automation** | ⬜ **PENDING, not built** — no automated detection of live-prompt vs `ROUTINE_DAILY_CYCLE.md` drift |
| 10 | **Devil's-Advocate-on-holdings nudge** | ⬜ **PARKED — your call** — deliberately held back over turnover cost |

---

### [ ] 22. Re-quote qty mismatch — ABSOLUTE vs DELTA — 🟠 **CODE FIXED, AWAITING ROUTINE SYNC**

**Canonical prompt corrected 2026-08-28** (`ROUTINE_DAILY_CYCLE.md` STEP 4). **The LIVE
routine still carries the bug** until the corrected block is pasted into the routines UI —
that is the only remaining action, and it is owner-only.

**It was two bugs, not one.** The routine re-quoted a stale-priced order as
`target_weight × total_value ÷ live_price` — an ABSOLUTE position — while
`execute._compute_qty()` returns a **DELTA**:

- **BUY of a name already held** → bought the full target *on top of* the existing position,
  roughly doubling the intended add, and breaching the sector cap at the broker for a
  clamped BUY.
- **SELL with `target_weight = 0`** (a full exit) → `0 × total ÷ price = 0`, and STEP 4's
  "skip if qty <= 0" then **silently skipped the exit entirely.** This was not in the
  original write-up.

**Scope — narrower than it first appears.** The re-quote only triggers when
`decision["price_as_of"]` is present and ≠ today, and only `main.py` stamps that field (from
the dossier, line ~745). **`risk_watch.py` never stamps it**, so the daily −25% stop-loss
path is NOT exposed — it always uses its own live MCP quote and the pre-computed qty. The
exposed surface is **rebalance decisions on a stale-price day only**.

**The corrected block** mirrors `_compute_qty` exactly, including the full-exit special case
(`target_weight == 0` → `available_qty`, never scaled by price). See `ROUTINE_DAILY_CYCLE.md`
STEP 4 for the paste-ready text.


### [ ] 25. Reset `portfolio_peak.json` if/when the account is funded — ⏸️ **PARKED 2026-08-26**

**No deposit is planned for now** (owner, 2026-08-26). The account stays at ~$500 and this
item is dormant — but it must re-arm the moment any money is added, so it is parked rather
than closed.

**Why it matters when it does happen:** `portfolio_peak.json` tracks raw `total_value`, so a
deposit looks like a new all-time high. The drawdown kill switch then measures the fall from
a peak that includes deposited cash rather than investment performance, and can arm itself
against a loss that never happened — blocking all new BUYs.

**The order matters:** deposit first, *then* set `peak` to the post-deposit value, *then*
let the next run happen. See DEPLOYMENT.md Runbook Scenario C.


## 🆕 PENDING (2026-08-22) — Nasdaq 100 benchmark

### [ ] 24. Coverage "regression" — DIAGNOSED 2026-08-22, two defects to fix — **not owner-only**

**Original symptom:** `data_quality_report.json` read `fundamental_coverage_pct` **72.9%**,
`status: DEGRADED`, `strategy_shift_ok: false` — apparently a coverage collapse blocking
Phase 2 of `PLAN_BETA_ALPHA_SPLIT.md`.

**It is not a coverage collapse.** Verified against `data_quality_history.jsonl` (119 rows),
four same-day committed snapshots, and the live GH Actions log for run `32486847810`:

| Time (UTC) | `universe_expanded` | names | coverage | status |
|---|---|---|---|---|
| ~10:1x | False | 102 | **96.1%** | OK |
| ~11:4x | True | 174 | 79.7% | DEGRADED |
| ~13:1x | False | 102 | **96.1%** | OK |
| ~13:5x | True | 174 | 72.9% | DEGRADED |

Core-universe coverage is **96.0%** (96/100 names carry real margin/leverage fields) — healthy
and consistent with the post-SEC-User-Agent-fix level. The 72.9% headline is a *blended*
number over a universe that only exists on alternate runs, and it is simply **the last run
of the day**, which is the one that lands in `data_quality_report.json`.

**Defect 1 — the universe gate is an oscillator with no hysteresis.**
`market_data._prior_coverage_ok()` reads the previously-**committed snapshot's**
`data_quality.coverage_ok`. Its docstring says "yesterday's fundamental-coverage verdict",
but `market_data.yml` fires **4 crons a day** (09:00/11:00/12:00/12:30 UTC) and each run
commits a new `market_snapshot.json` — so it actually reads the *previous run's* verdict.
`universe.get_active_universe(coverage_ok=...)` then flips: expanded → coverage below floor
→ next run contracts to core → coverage above floor → next run expands. A stable 2-cycle.

> **Trading-path consequence (the part that matters).** The candidate set the Portfolio
> Manager sees is **non-deterministic** — 102 or 174 names depending on which cron last
> committed before the 9:45 AM ET (13:45 UTC) routine. Aug 19 got the core snapshot
> (13:12 commit; the expanded 13:53 commit landed *after* the routine) — correct **by luck
> of cron timing, with roughly a 10-minute margin**. If GH Actions runs faster on any given
> day the routine consumes the expanded snapshot instead, putting ~41 momentum+vol-only
> names into the candidate pool — precisely what `get_active_universe`'s own docstring says
> the gate exists to prevent, and doubly bad given those two factors are the beta-loaded
> ones (`PLAN_BETA_ALPHA_SPLIT.md` §2).

**Defect 2 — coverage is measured over a moving target, so the gate can never re-open.**
`_compute_fundamental_coverage(all_tickers, ...)` measures over *this run's* `all_tickers` =
core(102) + the **rotating 75-name expansion batch** (log: `batch 75/288 ... cursor=150`,
advancing to 225). A given expansion name is only enriched when it is simultaneously (a) in
the current batch **and** (b) in today's alternate-day 50/50 group — roughly 1 run in 8 —
while its cache entry expires on a **2-day TTL**. Enrichment cannot keep pace with the
rotation, so the batch's own coverage sits near **44.6%** (33/74 verified on commit
`3f6cd25`) and the blend is pinned. Five days of evidence, zero convergence:

    8/17: 79.7 / 72.9   8/18: 78.2 / 74.0   8/19: 79.7 / 72.9
    8/20: 78.2 / 74.0   8/21: 79.7 / 72.9

A sweep genuinely filling in would climb. This is a limit cycle.

**Ruled out (verified, not assumed):**
- *Not* a cache-persistence failure — GH Actions log shows `Cache restored successfully`,
  2 MB, key `fundamentals-v2-Linux-233`, then `Cache saved with key: ...-234`.
- *Not* absent EDGAR data — the uncovered names are large US filers (LVS, MAA, MCO, MDLZ,
  MET, MGM, MKC, MMM) that certainly file, and they are **clustered in cursor order**,
  which is a sweep position, not a data gap.
- *Not* a `min_depth` or universe-fetch problem — 205 bars, 98.3–100% fetched throughout.

**Still unverified:** what happened on **2026-08-14**, when an expanded run dropped from the
94.4–95.8% it had sustained all of 8/13 to 44.6%, and core runs then read 45.1% (the ~50%
signature of a cold enrichment cache). The cache restores correctly *now*, so whatever
invalidated it has passed; CI logs for that date may have aged out. Recorded as unexplained
rather than guessed — the ORCL misdiagnosis (item 9) is the cautionary precedent.

**Impact on Phase 2:** **does not block it.** Phase 2's re-weight is measured on the core
universe, which is at 96.0%. The 80% floor should be evaluated against core coverage, not
against a rotating-batch blend.

**Fix options (needs a decision, not yet implemented):**
1. **Latch the universe decision once per calendar day** (read the first snapshot of the day,
   or stamp the decision) — smallest change, kills the oscillation, makes the routine's
   candidate set deterministic.
2. **Add hysteresis** — expand at ≥80%, contract only below ~70% — standard control fix.
3. **Gate on CORE-universe coverage** (arguably most correct): the gate's real question is
   "is the fundamentals pipeline healthy?", which is a property of the core universe, not of
   how far an in-progress expansion sweep has got. Blending sweep progress into the gate is
   what created the loop.
4. Separately, align the coverage **denominator** with what is actually enrichable, or widen
   the TTL / batch interaction so a name can be refreshed while it is in-batch.

⚠ **Do not ship any of these before the Wed 2026-08-26 rebalance** — it is the sector clamp's
first live fire (`PLAN_BETA_ALPHA_SPLIT.md` §6) and must not be confounded. Option 1 or 3
would change which universe that run sees.

### [ ] 11. `since_entry` dossier anchor is structurally always `None` — **PENDING, not fixed** (found 2026-07-05, Phase 1 dry-verify)
- The dossier's **entry anchor** (`_fmt_since_entry` → the "judge the position against entry,
  not last week" block the Stage C Position-Review agent reads, STRATEGY_REDESIGN_PLAN §13.3)
  renders `last_decision` fine but **never** the `since_entry` cumulative-return line — verified
  against both held names with open BUYs (AXP, EBAY): both show `since_entry=None`.
- **Root cause:** `build_dossier._last_decision` computes `since_entry` from
  `last.get("entry_price") or last.get("price")`, but `journal.record_trade()` has **no
  `entry_price`/`price` parameter at all** — every journal entry is written with those fields
  absent, so the guard `isinstance(entry_px, (int,float))` is always False. The feature has
  been inert since it shipped.
- **Fix (execution-adjacent — next batch, not Phase 1):** thread the executed/decision price
  into `record_trade()` at both call sites (`main.py`, `risk_watch.py`) — or, better, populate
  it from the broker fill during `mark_transactions_live` reconciliation so it reflects the
  REAL entry, not the decision-time quote. Touches the trade-journal write path → `/code-review
  high` + tests. Quietly defeats a headline Phase 5 Stage C mechanism until fixed.

### [ ] 14. Narrow the risk_watch cross-mode interlock — **PENDING, not built** (unblocked — the #13 observation window closed 2026-07; not urgent)
- Currently `_mirror_rebalance_stamp` (journal.py) records ALL rebalance-traded tickers — BUYs
  AND SELLs — and `risk_watch._interlocked_tickers` refuses to stop-loss-sell ANY of them for the
  rest of the ISO week. The interlock only needs to protect against double-selling a name the
  rebalance already SOLD; a name the rebalance just BOUGHT should still be protected by the daily
  −25% stop if it craters days later. **Fix:** key the mirror/interlock off rebalance SELLs only.
  This is a change to `risk_watch.py`'s decision set → real order-path code → `/code-review ultra`
  + a weekend dry-run before merging, per DEPLOYMENT.md §7.0. Not urgent (DEGRADED health still
  pages you if a fired-but-interlocked stop is ever hit); do this in the first quiet week.

### [ ] 15. Crash-evidence preservation in risk_watch — **PENDING, not built** (unblocked — the #13 observation window closed 2026-07)
- If a Wednesday rebalance crashes after claiming but before stamping `executed_at`, Thursday's
  `risk_watch.py` overwrites `pending_decisions.json` — destroying the exact envelope
  `reconcile.py`/Scenario B need to diff intended-vs-actual orders. Fix: `risk_watch.py` archives
  a claimed-but-unstamped prior envelope (e.g. to `pending_decisions.crashed.json`) before writing
  its own, and `reconcile.py` prefers the archived file when present. Small, testable, no change
  to risk_watch's own decision logic — but touches the same file the order path depends on, so
  still `/code-review high` minimum.

### [ ] 17. Prompt-drift automation — **PENDING, not built** (ready to build; needs a routine-prompt sync after)
- The recurring "requires a live-routine sync" failure class (this repo's most common operational
  incident — see the Jun 16/17 branch-execution and STEP-3/5 drift entries) has no automated
  detection: the only way to know the live prompt matches `ROUTINE_DAILY_CYCLE.md` is to manually
  diff it (as done for item 0 above). Fix: have the routine echo a short prompt-version string
  (e.g. a hash of the canonical .md, or a manually-bumped version line) into `system_health.json`;
  the heartbeat compares it against the current `ROUTINE_DAILY_CYCLE.md`'s stamped version and
  alerts on mismatch. Code is buildable now; taking effect requires pasting the updated prompt
  into the live routine same as any other prompt change.

### [ ] 10. The Devil's-Advocate-on-holdings nudge (`feat/pm-devil-tension`) — **PENDING, parked** (your call)
- A June-17 review branch surfaces the DA verdict on PM holdings lines. You deliberately held it
  back ("may increase turnover"). It can't be merged as-is (stale base). If you want it, say so
  and Claude will re-implement it on current `main` **with** the turnover/after-tax trade-off
  called out — turnover is ~54% short-term tax in this account.

---

---

## ✅ Closed archive

Resolved items, collapsed to one line each on **2026-08-22**. Full bodies remain in git
history (`git log -p -- MANUAL_TODO.md`). Each was re-verified in this session unless noted.

| # | Item | Resolution |
|---|------|------------|
| 23 | Nasdaq 100 (QQQ) benchmark migration + backfill | ✅ **DONE 2026-08-22** — DDL run by owner in the Supabase SQL Editor; `backfill_qqq.py` wrote 54 rows. Verified live on the dashboard chart |
| 21b | 35%-financials breach — age-out watch | ✅ **CLOSED 2026-08-19** — aged out as designed (Financials 25.06%); superseded by IPS §6.1 entry-time caps. Supersedes #21 |
| 21 | 35%-financials breach — monitor | ✅ **CLOSED** — superseded by #21b |
| 20 | Re-sync BOTH routine prompts (Jul 9 hardening) | ✅ **DONE 2026-07-10** — verified byte-for-byte via `RemoteTrigger(action="list")` |
| 19 | Owner decision — stop-loss IPS text vs implementation | ✅ **DECIDED 2026-07-05** (option a) — IPS/`policy.yaml`/CLAUDE.md/`risk_watch.py` all corrected to "evaluated each trading-day morning on a live MCP quote" |
| 18 | Owner decision — the deployment mandate | ✅ **DECIDED 2026-07-05** (option a) — defensive cash ratified in IPS §6 with a two-part review trigger and a Q1 2027 backstop |
| 16 | Score the PM's `expected_return` in calibration | ✅ **DONE 2026-07-05** — `calibration.log_pm_forecasts`, scored from the RAW pre-CRO proposal to avoid survivorship bias |
| 13 | Go-live observation checklist | ✅ **CLOSED** — window opened 2026-07-06 and ran its course; the system has traded live for seven weeks since. Nothing from the checklist outstanding |
| 12 | Merge `fix/phase1-hardening-evidence-clock` | ✅ **DONE 2026-07-05** — PR #27 |
| 9 | ORCL "split-unadjusted history" (P0-3) | ✅ **RESOLVED 2026-07-05** — investigated and found a **misdiagnosis**; `ret_21d ≈ −0.43` was a genuine decline. The proposed quarantine would have discarded real momentum signal. Now the cautionary precedent cited in `.claude/skills/data_steward/SKILL.md` |
| 8 | Dossier consumer wiring | ✅ **DONE 2026-07-04** — shipped as Phase 5 Stage C. Its one open sub-item was split out as #11 |
| 7 | Heartbeat + digest first scheduled runs | ✅ **CONFIRMED 2026-08-22** — `heartbeat.yml` has four consecutive successful runs (8/18–8/21). The stale local `heartbeat_report.json` is **by design**: it is gitignored as "transient — written only inside the heartbeat.yml run" |
| 6 | `UNIVERSE_EXPANDED` fetch-cursor wiring | ✅ **DONE 2026-07-05** — `market_data.select_fetch_batch`; both gating conditions met |
| 3 | PyYAML in the cloud routine environment | ✅ **MOOT 2026-08-22** — cannot be harmful by design: `policy.py` is deliberately tolerant, and a YAML/PyYAML failure falls back to `_DEFAULTS`, which `TestPolicyParity` asserts is byte-identical to `policy.yaml`. Locally `policy.policy_version()` returns `2.0-phase5-weekly`, so the loader path is sound |
| 0b | Flip `UNIVERSE_EXPANDED` for the ~400-name universe | ✅ **DONE — was stale in this file.** Verified live 2026-08-22: `market_snapshot.json` carries `universe_expanded: true` and the GH Actions log reads `UNIVERSE_EXPANDED: true`, `EXPANDED universe active: 390 total`. On since ~2026-08-13. See #24 for the oscillation this exposed |
| 0 | Daily routine prompt sync | ✅ **DONE** — verified byte-for-byte |

---

_Maintained by Claude as new owner-only steps arise. The `[x]`/`[ ]` checkbox tracks the item
AS A WHOLE; the title tag gives the finer-grained read for compound items. "Verified" always
means checked against a real artifact, workflow run, or API response in this repo — never
assumed from memory. When an item closes, move it to the archive above in the same commit
rather than leaving a resolved item in the open list._
