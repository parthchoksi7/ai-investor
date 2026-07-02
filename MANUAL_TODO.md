# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
Newest concern first. Check items off as you do them.

---

## 🔴 Required before Phase 1 actually persists in the cloud

### [ ] 1. Sync the live DAILY routine prompt
- **Why:** `ROUTINE_DAILY_CYCLE.md` now adds the forecast + counterfactual ledgers
  (`forecasts.jsonl`, `forecasts_scored.jsonl`, `agent_scorecards.json`,
  `decisions_ledger.jsonl`, `decisions_scored.jsonl`, `counterfactual.json`) to the STEP 4
  (claim) and STEP 5 (daily-cycle) `git add` lines. **The live routine prompt is NOT
  auto-updated by a repo change.** Until you sync it, the cloud run writes the ledgers but
  the `git add` omits them → the evidence clock still won't persist from the cloud (the exact
  bug Phase 1 fixed, just one layer up).
- **How:** routines UI → `YOUR_ROUTINE_ID_DAILY` → paste the updated STEP 4 + STEP 5 `git add`
  lines from `ROUTINE_DAILY_CYCLE.md`, substituting the real secrets (`POLYGON_API_KEY`,
  `SUPABASE_SERVICE_KEY`, account number) that are redacted in the repo.
- **Why Claude can't:** no access to the secrets; editing a live scheduled routine is an
  account action.
- **Also fold in (if not already synced from earlier revs):** STEP 0 `git checkout -B main
  origin/main`, STEP 1 `as_of`, STEP 4 claim-commit-push + per-order try/except. Diff your
  live prompt against `ROUTINE_DAILY_CYCLE.md` to be sure.

### [ ] 2. Sync the live EOD routine prompt (only if changed)
- `ROUTINE_EOD_CLOSE.md` — diff against the live `YOUR_ROUTINE_ID_EOD` prompt; sync if drifted.
  (No Phase 1 change here, but verify it's current.)

---

## 🟠 Before Phase 2 (data layer)

### [ ] 3. Verify PyYAML is installed in the cloud routine environment
- **Why:** Phase 0's `policy.py` loader **silently falls back to built-in defaults** if PyYAML
  is missing. Harmless for Phase 0 (defaults == the shipped `policy.yaml` values), but Phase 2
  is the first phase that **changes** a policy value (`price_outlier_pct`, `policy_version`
  → `1.1-phase2-dataquality`) — a cloud env without PyYAML would keep using the old default
  ("works locally, silent no-op in prod").
- **How:** confirm the routine's `pip install -r requirements.txt` step succeeds with `PyYAML`
  (now in `requirements.txt`), or check a cloud run log for a `⚠ policy.yaml not loaded` warning.
  If it warns, fix the cloud env before Phase 2 lands.

### [ ] 6. Confirm ≥80% fundamental coverage in GH Actions BEFORE enabling universe expansion
- **Why:** the ~400-name expansion (`universe.EXPANDED_UNIVERSE`) is **gated OFF by default**
  (`UNIVERSE_EXPANDED` env unset → active universe stays the core 100). A wider pool on thin
  coverage just adds momentum-only names with no quality/value signal. Enable it ONLY after the
  `market_data.yml` logs show `Fundamental coverage: ≥80%` (and `data_quality.coverage_ok=true`)
  for several consecutive runs. The coverage-swallow fix (Phase 2) is what makes that reachable —
  but SEC EDGAR blocks residential IPs, so **coverage can only be verified from GH Actions logs,
  not locally**.
- **How:** once coverage clears, set `UNIVERSE_EXPANDED=true` in the `market_data.yml` env.
- **Note — cursor wiring is Phase 4:** `universe.next_batch/save_batch` (the resumable
  `fetch_progress.json` cursor) is built + tested but **not yet wired into the fetch loop**.
  Fetching 400×210-day histories under Polygon's 5-calls/min needs the batch-fetch + history
  carry-forward (previous snapshot merged with today's batch), which is the Phase 4 raw→curated
  storage split (§12). Do not flip `UNIVERSE_EXPANDED` on before that wiring lands, or a run would
  fetch only a partial universe.

---

## 🟢 Merges / decisions (Claude CAN do these — just say so)

### [ ] 4. Merge PR #17 (Phase 1) after the code review
- merge = deploy (the routine pulls `main`). Your call. Say "merge phase 1" and I'll do it.

### [ ] 5. (Optional) Settle the two parked IPS open questions
- Both already have sensible defaults applied; only revisit if you disagree with them.

---

_Maintained by Claude as new owner-only steps arise. Items move to "done" by deletion or a checked box._
