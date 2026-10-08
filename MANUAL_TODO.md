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
| 33 | **Run the benchmark-close backfill** (after the `fix/benchmark-close-staleness` PR is merged). `git pull`, then `python backfill_benchmark_closes.py` (dry run: lists every row whose SPY/QQQ close does not match the dated bar, old → new close and cumulative return, and writes `evidence/benchmark_close_backfill_<date>.json`). Inspect it — expect about 34 lagged trading-day rows from 2026-08-07 on plus the NULL 2026-08-11, and no holiday row (06-19, 07-03, 09-07) — then `python backfill_benchmark_closes.py --apply` and commit the evidence file. Needs `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`; with `POLYGON_API_KEY` set it cross-checks the snapshot's bars against Polygon | ⬜ **OWNER** — writes to the production Supabase, so it is not run from a Claude session. Until it runs, the per-publish repair fixes only the previous 14 weekday rows; older rows stay one session behind |
| 34 | **Check the first evening benchmark repair runs** (`benchmark_reconcile.yml`, 7 PM ET weekdays — added in pre-merge review 2026-10-08, because the website's headline "vs SPY / vs QQQ" reads only the newest row and would otherwise be blank every evening and all weekend). It runs `python publish.py --reconcile-benchmarks` and writes only the four benchmark columns of existing rows. **Not yet verified:** that Polygon's free tier has the day's bar by 7 PM ET. If the run logs "waiting on the <date> bar" on most evenings, move the cron later; if you would rather not have the job, delete the workflow file — the morning publish still fills the row (~1 Actions minute per weekday either way) | ⬜ **OWNER** — look at the first two or three runs' logs; no action if they show "corrected" for today's row |
| 26 | **Live-routine sync for the Oct 3 remediation** | ✅ **DONE 2026-10-05** — owner re-pasted ~23:47 ET; verified via the routines API: the live daily prompt matches `ROUTINE_DAILY_CYCLE.md` line-for-line (secrets normalized), including the FINAL-ATTEMPT FALLBACK block. Schedule and enabled state unchanged |
| 27 | **GitHub → Settings → Notifications → Actions: email on failed workflows** | ✅ **DONE 2026-10-04** (owner) — the only alert path that still works when Actions itself is blocked (Sep 21–30 2026 billing outage: heartbeat, alert, publish all dark, zero alerts for 8 days) |
| 28 | **`/code-review ultra` on the Oct 3 remediation PR** (execution-path items 1.2/1.3) | ✅ **DONE 2026-10-04** — 15 findings (2 pre-existing P0s: kill switch + circuit breaker never acted on the routine's envelope); fixed, re-reviewed, merged as `e6de46e` |
| 29 | **Verify 11 universe names absent from SEC's ticker map** (2026-10-03): ANSS, AVB, BK, DFS, EA, EQR, HES, JNPR, MMC, PXD, WBA — all also absent from the snapshot. Some are known acquisitions (ANSS, DFS, HES, JNPR, PXD, WBA); others may be renames that silently dropped a live company from the universe | ⬜ **data_steward** — verify each against Polygon reference before editing `universe.py` (verify-before-fix: the ORCL lesson) |
| 30 | **Phase 5 — grouped-daily fetch** (`PLAN_OCT3_REMEDIATION.md` §5): one Polygon call per day for every ticker instead of ~200 rate-limited calls | ⬜ flag-gated, default off; turning it on is an owner decision (data substrate change) |
| 31 | **Explicit final-attempt flag** (ultra review, altitude): "final attempt" is inferred from a 12:40 ET wall-clock that must track the routine cron + its manual DST updates. Have the routine pass the attempt index instead | ⬜ follow-up — a missed DST update only disables the fallback (no worse than before it existed) |
| 32 | **Ticker identity check** (ultra review, altitude): `is_history_young` catches a reused symbol only within ~126 sessions of reassignment. Check universe entries against SEC CIK / Polygon reference identity instead (pairs with #29) | ⬜ follow-up — data_steward |
| 22 | **Re-quote qty mismatch: absolute vs delta** | ✅ **CLOSED 2026-08-28** — canonical prompt fixed AND live routine synced; STEP 4 block verified matching byte-for-byte |
| 25 | **Reset `portfolio_peak.json` if/when the account is ever funded** | ⏸️ **PARKED 2026-08-26** — no deposit planned for now. Re-arms the moment one is made. **Also (2026-10-04):** record the deposit in `cash_flows.json` (`{"YYYY-MM-DD": amount}`) — the daily-close performance curve (and the published realized_beta) treats an unrecorded deposit as a +100% day |
| 24 | **Coverage "regression" — DIAGNOSED, not a data failure** | ✅ **Hysteresis shipped 2026-10-03** (enter ≥80%, stay ≥70%). Denominator left as-is: coverage reads 98.2% expanded today. The post-outage 175↔164 universe swing is carry-forward expiry during batch catch-up, not the gate — Phase 5 (#30) removes it |
| 11 | **`since_entry` dossier anchor is structurally always `None`** | 🟡 **PENDING** — re-verified 2026-08-22: 172 dossier records, **0** non-null. Inert since it shipped |
| 14 | **Narrow the risk_watch cross-mode interlock** | ✅ **DONE 2026-10-04 (PR #48)** — `last_rebalance.json` gains `sell_tickers`; risk_watch interlocks only names the rebalance SOLD (legacy files without the key keep the old, wider set). Became urgent once cash-deployment top-ups would have switched off the Thu/Fri stop for most of the book |
| 15 | **Crash-evidence preservation in risk_watch** | ⬜ **PENDING, not built** — archive a claimed-but-unstamped envelope before overwriting |
| 17 | **Prompt-drift automation** | ⬜ **PENDING, not built** — no automated detection of live-prompt vs `ROUTINE_DAILY_CYCLE.md` drift |
| 10 | **Devil's-Advocate-on-holdings nudge** | ⬜ **PARKED — your call** — deliberately held back over turnover cost |

---

### [x] 22. Re-quote qty mismatch — ABSOLUTE vs DELTA — ✅ **CLOSED 2026-08-28**

Canonical prompt corrected **and the live routine synced by the owner the same day**. The
STEP 4 re-quote block was verified **matching the canonical `ROUTINE_DAILY_CYCLE.md`
byte-for-byte**, including the full-exit special case.

**It was two bugs, not one.** The routine re-quoted a stale-priced order as
`target_weight × total_value ÷ live_price` — an ABSOLUTE position — while
`execute._compute_qty()` returns a **DELTA**:

- **BUY of a name already held** → bought the full target *on top of* the existing position,
  roughly doubling the intended add, and breaching the sector cap at the broker for a
  clamped BUY.
- **SELL with `target_weight = 0`** (a full exit) → `0 × total ÷ price = 0`, and STEP 4's
  "skip if qty <= 0" then **silently skipped the exit entirely.** Not in the original
  write-up; found while drafting the fix.

**Scope, verified rather than assumed:** the re-quote only triggers when
`decision["price_as_of"]` is present and ≠ today, and only `main.py` stamps that field.
`risk_watch.py` never does, so the daily −25% stop-loss path was **never exposed** — it
always uses its own live MCP quote. The exposed surface was rebalance decisions on a
stale-price day only.

**Never observed firing live** — no envelope has yet carried a stale `price_as_of`. Fixed
before it cost anything.


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
