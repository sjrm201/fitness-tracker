-- ============================================================
-- 1. DAILY METRICS: one row per metric per day (long format)
-- ============================================================
create table daily_metrics (
  day         date        not null,              -- your local calendar day, e.g. 2026-10-15
  metric      text        not null,              -- e.g. 'protein', 'heart_rate_variability'
  value       numeric     not null,              -- the number; missing data = no row at all
  unit        text,                              -- 'g', 'kcal', 'ms', 'hr', 'count'
  source      text,                              -- 'MyFitnessPal', 'Ultrahuman', ...
  updated_at  timestamptz not null default now(),-- when this row was last written
  primary key (day, metric)                      -- one value per metric per day, ever
);

-- ============================================================
-- 2. WORKOUTS: one row per workout session
-- ============================================================
create table workouts (
  id               text        primary key,      -- Apple Health's unique workout ID
  start_at         timestamptz not null,
  end_at           timestamptz not null,
  activity_type    text        not null,         -- 'functional_strength_training', 'martial_arts', 'cool_down', ...
  name             text,                         -- 'Functional Strength Training'
  duration_min     numeric,
  active_kcal      numeric,
  avg_hr           numeric,                      -- Polar: recalculated without strap dropouts
  max_hr           numeric,
  hr_coverage_pct  numeric,                      -- % of minutes with a real HR signal (NULL = no HR data)
  distance_km      numeric,                      -- NULL for non-distance workouts
  source           text,                         -- 'Apple Watch', 'Polar Beat'
  updated_at       timestamptz not null default now()
);

-- ============================================================
-- 3. RAW PAYLOADS: every delivery kept exactly as received
-- ============================================================
create table raw_payloads (
  id           bigint generated always as identity primary key, -- auto-numbered 1, 2, 3...
  received_at  timestamptz not null default now(),
  automation   text,                             -- which Auto Export automation sent it
  payload      jsonb       not null              -- the full JSON, untouched
);

-- ============================================================
-- 4. DAILY NOTES: free-text context, one per day
--    (body weight arrives from Apple Health as metric 'weight_&_body_mass')
-- ============================================================
create table daily_notes (
  day         date        primary key,
  note        text        not null,               -- empty note = no row
  updated_at  timestamptz not null default now()
);

-- ============================================================
-- 5. LOCK IT: RLS on, no rules = only the secret key gets in
-- ============================================================
alter table daily_metrics enable row level security;
alter table workouts      enable row level security;
alter table raw_payloads  enable row level security;
alter table daily_notes   enable row level security;

-- ============================================================
-- CHANGE LOG (migrations already applied to the live database)
-- ============================================================
-- 2026-10-20: alter table workouts add column hr_coverage_pct numeric;
-- 2026-10-25: create table daily_notes (...); alter table daily_notes enable row level security;
