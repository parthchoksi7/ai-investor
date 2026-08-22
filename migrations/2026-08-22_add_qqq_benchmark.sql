-- Nasdaq 100 (QQQ) benchmark alongside the existing S&P 500 (SPY) benchmark.
--
-- The dashboard benchmarks the portfolio against SPY only. The strategy's
-- universe skews large-cap growth, so SPY alone understates the opportunity
-- cost of the tilt — QQQ is the harder, more honest comparison. These columns
-- mirror spy_close / spy_cumulative_return_pct exactly:
--   qqq_close                 — QQQ close for the session the row was priced from
--   qqq_cumulative_return_pct — TOTAL return (%) vs inception, price return from
--                               closes + a dividend gross-up by days elapsed
--
-- Run once in the Supabase SQL Editor. Safe to re-run.
alter table public.portfolio_snapshots
  add column if not exists qqq_close                 numeric,
  add column if not exists qqq_cumulative_return_pct numeric;
