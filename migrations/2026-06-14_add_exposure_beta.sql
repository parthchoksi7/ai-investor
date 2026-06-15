-- Migration: A4 — surface net exposure + realized beta on the dashboard.
--
-- The portfolio holds cash (8–15 names + dividend/residual cash), so raw return
-- vs a fully-invested SPY is not risk-matched. These two columns let the
-- dashboard report average net exposure and realized beta alongside the
-- (now total-return) SPY comparison. See performance.py / publish.py.
--
-- HOW TO RUN: paste into Supabase → SQL Editor → New query → Run.
-- Idempotent (IF NOT EXISTS), so it is safe to run more than once.

ALTER TABLE public.portfolio_snapshots
  ADD COLUMN IF NOT EXISTS net_exposure  numeric;   -- 1 − cash/total_value (point-in-time)

ALTER TABLE public.portfolio_snapshots
  ADD COLUMN IF NOT EXISTS realized_beta numeric;   -- trailing beta vs SPY total return

-- service_role already has table-level DML grants (see schema.sql); column adds
-- inherit them, so no extra GRANT is required. Re-stated here for clarity:
GRANT SELECT, INSERT, UPDATE, DELETE ON public.portfolio_snapshots TO service_role;
