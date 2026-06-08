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
  created_at                timestamptz default now()
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
