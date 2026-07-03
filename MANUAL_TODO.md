# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
Newest concern first. Check items off as you do them.

_Last refreshed: 2026-07-02 (Phases 2 + 3 deployed to `main`; Phase 4 producer landing)._

---

## 🔴 Live routine prompt sync (cannot be done from code)

### [ ] 1. Sync the live DAILY routine prompt with `ROUTINE_DAILY_CYCLE.md`
- **Why:** the live Anthropic routine prompt is **NOT** auto-updated by a repo change. The
  canonical `ROUTINE_DAILY_CYCLE.md` STEP 4/5 `git add` lines list every artifact the cloud
  run must persist (the forecast + counterfactual ledgers `forecasts.jsonl`,
  `forecasts_scored.jsonl`, `agent_scorecards.json`, `decisions_ledger.jsonl`,
  `decisions_scored.jsonl`, `counterfactual.json`). Until the live prompt matches, the cloud
  run writes those files but the `git add` omits them → the evidence clock never persists.
- **Phase 3 note:** the new `data_quality` health check + provenance stamp ride *inside*
  `system_health.json` / `pending_decisions.json` / `forecasts.jsonl`, which the routine
  **already** commits — so **Phase 3 adds NO new routine-sync requirement.** No change needed
  for Phase 3.
- **How:** routines UI → `YOUR_ROUTINE_ID_DAILY` → paste the STEP 4 + STEP 5 `git add` lines
  from `ROUTINE_DAILY_CYCLE.md`, substituting the real redacted secrets (`POLYGON_API_KEY`,
  `SUPABASE_SERVICE_KEY`, account number). Diff your live prompt against the MD to be sure
  STEP 0 `git checkout -B main origin/main`, STEP 1 `as_of`, and STEP 4 claim-commit-push are
  all present.
- **Why Claude can't:** no access to the secrets; editing a live scheduled routine is an
  account action.

### [ ] 2. Sync the live EOD routine prompt (only if drifted)
- Diff `ROUTINE_EOD_CLOSE.md` against the live `YOUR_ROUTINE_ID_EOD` prompt; sync if drifted.
  No Phase 2/3/4 change here, but verify it's current.

---

## 🟠 Data-layer gates (Phase 2 — deployed) + universe expansion

### [ ] 3. Verify PyYAML is installed in the cloud routine environment
- **Why:** `policy.py` **silently falls back to built-in defaults** if PyYAML is missing.
  Phase 2 shipped `policy_version → 1.1-phase2-dataquality` and `price_outlier_pct` — a cloud
  env without PyYAML keeps using the old defaults ("works locally, silent no-op in prod").
- **How:** confirm the routine's `pip install -r requirements.txt` succeeds (`PyYAML` is in
  `requirements.txt`), or check a cloud run log for a `⚠ policy.yaml not loaded` warning.

### [ ] 6. `UNIVERSE_EXPANDED` — coverage gate now MET; cursor wiring still pending
- **Status update (2026-07-02):** condition (a) is **satisfied** — GH Actions logs now show
  **96% fundamental coverage, `data_quality.coverage_ok=true`** (the SEC User-Agent 403 fix).
  Condition (b) is **NOT yet met**: the resumable fetch cursor (`universe.next_batch/
  save_batch`) is built + tested but **still not wired into the fetch loop** (Phase 4 raw→
  curated storage split, §12). Fetching ~400×210-day histories under Polygon's 5-calls/min
  needs the batch-fetch + history carry-forward first.
- **Do NOT set `UNIVERSE_EXPANDED=true` yet** — a run would fetch only a partial universe.
  Flip it (in the `market_data.yml` env) only after the Phase 4 cursor wiring lands AND you've
  seen coverage hold ≥80% over the *expanded* set.

---

## 🟡 Phase 3 (observability — deployed): monitor, no action required

### [ ] 7. Watch the two NEW workflows' first scheduled runs
- `heartbeat.yml` (weekdays 6 PM ET) + `pipeline_digest.yml` (Fri 6:30 PM ET) auto-activate on
  merge — no setup needed. Just confirm their first *scheduled* (not dispatch) runs go green in
  the Actions tab, and that a `heartbeat-alert` issue opens/closes correctly the first time an
  artifact is genuinely missing. **DST:** both new crons are intentionally left OUT of
  `update_dst.yml` — a 1-hour seasonal drift is harmless (both fire after the 4 PM close).
- Permissions are declared in-workflow (`issues: write` / `contents: write`); no repo-setting
  change required.

---

## 🔵 Phase 4 (research pipeline — landing incrementally)

### [ ] 8. The dossier is PRODUCER-ONLY so far — the consumer change is a FUTURE routine sync
- **What shipped (increment 1):** `build_dossier.py` builds + schema-validates + commits
  `research_dossier.json` from GH Actions (zero order code — a research artifact only). It does
  NOT yet drive any decision.
- **Not yet done (later increment, WILL need a routine-prompt sync):** having the cloud routine
  read `research_dossier.json` instead of the raw snapshot, with a gate freshness check
  (`dossier.as_of == today` AND `built_from_days ≥ 2` → else SKIP/RETRY). That is an
  execution-adjacent change and must be coordinated with the Phase 5 weekly cadence — do not
  wire the consumer piecemeal.
- **Increment 2 shipped — the Haiku event digest** (`event_digest.py`, `events.jsonl`): now
  runs as Step 4 of the GH-Actions fetch and feeds the dossier. **Manual check:** confirm the
  `ANTHROPIC_API_KEY` Actions secret is present (it is per the Jun-9 incident log) — if unset,
  the digest self-skips (events stay empty; the dossier still builds). **Token cost:** the
  digest adds Anthropic spend to `market_data.yml` (Haiku, chunked 20/call, cached) — the §15.2
  token-budget cap + alert (P2-13) is still deferred; watch the first few runs' cost. A parse
  failure ≥20% now floors `data_quality_report.json` at DEGRADED (→ cloud health check → alert).
  **Accepted limitations (documented, not bugs):** (i) `events.jsonl` is committed to git and
  appended forever — it joins `factor_history` in the §12.4 storage-split/retention work (the
  dedup read scans the whole file, bounded logically to a 60-day window). (ii) The digest is
  LLM news summarization — a crafted headline tagged to a real ticker can yield a fabricated
  "material" event with a structured veneer; severity is low (enrichment-only, and the same raw
  feed already reaches the agents), but treat dossier events as leads, not facts.
- **Increment 3 shipped — `_as_of_filing` stamping** (`data_providers.SECProvider`): SEC
  fundamentals now carry the 10-K `filed` date (the no-look-ahead availability date), so the
  dossier reports REAL `fundamentals_age_days` / `fundamentals_stale` (was `null`) and the
  future-filing look-ahead drop is now LIVE, not inert. Note: FMP-covered names (~35%) still
  lack a filing date (FMP TTM has no single filing); those report vintage-unknown, which is
  honest. Old `provider_cache.json` entries backfill `_as_of_filing` on their normal TTL refresh.
- **Deferred sub-workstreams (documented, not built):** per-lot FIFO tax dates (P0-4).
- **Deferred `/code-review high` findings (non-correctness — tracked, not blocking):**
  (a) **storage wall (§12.4):** `research_dossier.json` is committed whole to git daily and grows
  with the universe — the planned raw→curated storage split (dossier to object storage / compact
  digest only) should land before the 400-name expansion. (b) **efficiency:** `build_dossier`
  loads the entire (unbounded) `factor_history.jsonl` + double-reads the snapshot from disk — fine
  now, revisit with the storage split (tail-read the recent window; pass the in-memory snapshot).
  (c) **reuse:** `_read_jsonl` / `_load_json` / atomic-write / `_max_drawdown` are duplicated across
  `data_quality` / `pipeline_digest` / `build_dossier` / `health` / `journal` / `performance` — a
  shared `io_utils` helper is warranted in a dedicated cleanup PR. (d) **tunables:**
  `_PERSISTENCE_WINDOW` / `_FUNDAMENTALS_STALE_DAYS` (and `market_data.FUNDAMENTAL_COVERAGE_FLOOR_PCT`)
  should migrate into `policy.yaml` for the single-source-of-truth invariant.

### [ ] 9. Known live DATA issue surfaced by the dossier — split-unadjusted history (P0-3)
- The dossier's `history_summary` shows e.g. ORCL `ret_21d ≈ −0.43` — a real artifact of
  **split-unadjusted OHLCV** in the snapshot (`corporate_actions.detect_price_outliers` already
  flags ORCL's ~36% one-day jump). Momentum/vol on such a series is corrupted. The Phase 4/§11.4
  fix is to assert Polygon history is split/dividend-adjusted (the fetch already sends
  `adjusted=true`, so confirm why ORCL slipped through) + the delisting/M&A handler for held
  names. **Not a dossier bug — a data-source correctness item to run down before the dossier
  drives trades.**

---

## 🟢 Merges / decisions (Claude CAN do these — just say so)

### [ ] 5. (Optional) Settle the two parked IPS open questions
- Both already have sensible defaults applied; only revisit if you disagree with them.

### [ ] 10. (Parked, your call) The Devil's-Advocate-on-holdings nudge (`feat/pm-devil-tension`)
- A June-17 review branch surfaces the DA verdict on PM holdings lines. You deliberately held it
  back ("may increase turnover"). It can't be merged as-is (stale base). If you want it, say so
  and Claude will re-implement it on current `main` **with** the turnover/after-tax trade-off
  called out — turnover is ~54% short-term tax in this account.

---

_Maintained by Claude as new owner-only steps arise. Items move to "done" by deletion or a checked box._
