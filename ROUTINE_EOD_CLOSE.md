# EOD Close Snapshot — Routine Prompt (canonical reference)

This is the authoritative copy of the **EOD Close Snapshot** Anthropic routine prompt
(`YOUR_ROUTINE_ID_EOD`). The live prompt lives in Anthropic's systems; keep this
file in sync whenever you change either one.

- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_EOD
- **Schedule (cron):** `0 20 * * 1-5` — 4:00 PM **EDT**, Mon–Fri.
  - **Winter (EST, set in November):** `0 21 * * 1-5`. Revert in March.
- **Places no orders.** Records the official 4:00 PM closing portfolio value to Supabase.
- **No API secrets belong in this prompt.** This routine can't reach Supabase (403), so
  `POLYGON_API_KEY` / `SUPABASE_*` are unused here (STEP 2 explains why); the real Supabase
  write runs in GitHub Actions with the GitHub secret store. Keep the prompt secret-free.

## ⚠️ The two non-obvious requirements that make this routine actually publish

`publish.yml` (GitHub Actions, which has Supabase access — the cloud routine does not) is the
only path that writes `close_value` to Supabase. It triggers **only** on a push that changes
`portfolio_snapshot.json`:

```yaml
on:
  push:
    paths:
      - portfolio_snapshot.json
```

Therefore STEP 4 **must**:

1. **`git add portfolio_snapshot.json`** — this is the file `publish.py --close` writes the close
   data into (`is_close: true` → `close_value`). Committing only `mcp_portfolio.json` (a past bug)
   silently never triggers `publish.yml`, so the close never reaches Supabase.
2. **Commit message must NOT contain `[skip ci]`** — `[skip ci]` tells GitHub Actions to skip all
   workflows on that push, suppressing `publish.yml` even when the right file is staged.

`publish.yml` runs `python publish.py` **without** `--close`; it reads `is_close` from the
committed `portfolio_snapshot.json` (guarded by the `GITHUB_ACTIONS` env var in `publish.py`),
so committing that file with `is_close: true` is what makes the Action write `close_value`.

---

```
Run the EOD Close Snapshot — record the official 4 PM closing portfolio value to Supabase. No trades, no analysis.

STEP 0 — Operate on main
The worktree may start on an arbitrary branch (e.g. claude/…). A bare git push targets the
CURRENT branch, so without switching to main the closing snapshot never reaches main's canonical
state and you may run a stale publish.py. Force the working tree onto the latest main first:
git fetch origin main
git checkout -B main origin/main

STEP 1 — Get portfolio via Robinhood MCP
Call:
- get_accounts() — confirm account YOUR_ACCOUNT_NUMBER is present
- get_portfolio(account_number='YOUR_ACCOUNT_NUMBER') — for cash and total_value
- get_equity_positions(account_number='YOUR_ACCOUNT_NUMBER') — for holdings
- get_equity_quotes for each held ticker (to get current_price)

Write mcp_portfolio.json in this exact format:
{
  "as_of": "<ISO-8601 timestamp, US/Eastern, e.g. 2026-06-12T16:00:00-04:00>",
  "cash": <float>,
  "total_value": <float>,
  "positions": [
    {"symbol": "TICKER", "qty": <float>, "avg_price": <float>, "current_price": <float>, "market_value": <float>, "unrealized_pnl": <float>}
  ]
}

unrealized_pnl = market_value - (avg_price * qty)
as_of = current ET timestamp at fetch time. (The EOD path reads this file via publish.py, which
does not enforce freshness, but write it anyway for consistency with the daily cycle and audit.)

STEP 2 — Set up environment
Create .env:
DRY_RUN=true

NO API SECRETS ARE NEEDED HERE — and none should be pasted into this prompt. This routine
places no orders and cannot reach Supabase (403). `publish.py --close` writes
portfolio_snapshot.json (with is_close/close_value) and then, with no keys, prints
"Supabase not configured — skipping" and returns cleanly; the committed portfolio_snapshot.json
push triggers publish.yml in GitHub Actions, which does the REAL Supabase write using the
GitHub Actions secret store. POLYGON_API_KEY is likewise unused here. Keep the prompt secret-free.

Install dependencies:
pip install -r requirements.txt -q

STEP 3 — Publish closing snapshot to Supabase
python publish.py --close

The --close flag writes both total_value (latest) AND close_value + close_at (the official EOD
closing price) into portfolio_snapshot.json. close_value is immutable once written — it is the
authoritative daily closing price used for the performance chart. Do not run this script without
--close from this routine.

STEP 4 — Commit the closing snapshot
git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add portfolio_snapshot.json mcp_portfolio.json
git diff --staged --quiet || git commit -m 'chore: eod portfolio snapshot'
git push

CRITICAL: STEP 4 MUST stage portfolio_snapshot.json and the commit message MUST NOT contain
[skip ci]. publish.yml triggers only on a portfolio_snapshot.json change and is skipped by
[skip ci]; either mistake means close_value never reaches Supabase.

DST note: This routine runs at 20:00 UTC (4:00 PM EDT). In November when clocks fall back to EST
(UTC-5), update the cron to `0 21 * * 1-5`. Back to `0 20 * * 1-5` in March.
```

---

## What changed — Jun 12 2026

1. **STEP 4 — stage `portfolio_snapshot.json`, not just `mcp_portfolio.json`.** The live routine
   committed only `mcp_portfolio.json`, so `publish.yml` (which triggers solely on a
   `portfolio_snapshot.json` push) never fired and the EOD `close_value` never auto-published.
   This is why daily-close points appeared only after a manual workflow dispatch / backfill commit
   (e.g. `ab844cc`).
2. **STEP 4 — removed `[skip ci]`** from the commit message (was `chore: eod portfolio snapshot
   [skip ci]`), consistent with the daily-cycle fix in `dde9b84`. `[skip ci]` suppressed
   `publish.yml` on the routine's push.
