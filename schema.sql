-- AI Investor — Supabase schema
-- Run this once in the Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Daily portfolio snapshots (one row per trading day)
create table public.portfolio_snapshots (
  date                      date        primary key,
  total_value               numeric     not null,
  cash                      numeric,
  num_positions             integer     default 0,
  cumulative_return_pct     numeric     default 0,
  spy_close                 numeric,
  spy_cumulative_return_pct numeric,
  drawdown_pct              numeric     default 0,
  regime                    text,
  run_id                    text,
  created_at                timestamptz default now(),
  updated_at                timestamptz default now()
);

-- All executed trades (append-only, one row per order)
create table public.trades (
  id                   text        primary key,
  date                 date        not null,
  ticker               text        not null,
  action               text        not null,
  qty                  numeric,
  price                numeric,
  total_value          numeric,
  target_weight        numeric,
  regime               text,
  rationale            text,
  research_confidence  integer,
  created_at           timestamptz default now()
);

-- Current positions (replaced wholesale each run — not append-only)
create table public.positions (
  ticker         text        primary key,
  weight_pct     numeric,
  quantity       numeric,
  avg_cost       numeric,
  current_price  numeric,
  unrealized_pct numeric,
  entry_date     date,
  updated_at     timestamptz default now()
);

-- Enable RLS on all tables (blocks anon key access entirely — defense-in-depth)
alter table public.portfolio_snapshots enable row level security;
alter table public.trades              enable row level security;
alter table public.positions           enable row level security;

-- service_role key (used by publish.py and the Next.js API route) bypasses RLS by design.
-- No anon SELECT policies — the website reads through the server-side API route only.

-- Explicit grants for the new Supabase secret key format (sb_secret_...)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.portfolio_snapshots TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.trades               TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.positions            TO service_role;

-- Migration: add broker_order_id to trades (run once if table already exists)
-- ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS broker_order_id text;
-- ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS dry_run boolean default false;

-- Migration: add updated_at to portfolio_snapshots (run once if table already exists)
-- ALTER TABLE public.portfolio_snapshots ADD COLUMN IF NOT EXISTS updated_at timestamptz default now();

-- Daily quant scores for all tickers (one row per date × ticker, for backtesting comparison)
-- Run once in Supabase SQL Editor to create the table:
-- CREATE TABLE IF NOT EXISTS public.quant_scores (
--   date        date          NOT NULL,
--   ticker      text          NOT NULL,
--   rank        integer,
--   composite   numeric(5,1),
--   momentum    numeric(5,1),
--   quality     numeric(5,1),
--   valuation   numeric(5,1),
--   volatility  numeric(5,1),
--   return_1m   numeric(6,2),
--   return_3m   numeric(6,2),
--   return_6m   numeric(6,2),
--   ann_vol     numeric(5,1),
--   beta        numeric(5,2),
--   PRIMARY KEY (date, ticker)
-- );
-- GRANT SELECT, INSERT, UPDATE, DELETE ON public.quant_scores TO service_role;
