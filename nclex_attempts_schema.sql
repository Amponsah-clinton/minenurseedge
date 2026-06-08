-- NCLEX practice test attempts (shared leaderboard per question group).
-- Run in Supabase SQL editor.

create table if not exists public.nclex_attempts (
  id uuid primary key default gen_random_uuid(),
  student_id uuid not null references public.profiles (id) on delete cascade,
  group_index integer not null check (group_index >= 1),
  correct_count integer not null check (correct_count >= 0),
  total_questions integer not null check (total_questions >= 1),
  percentage numeric(5, 2) not null check (percentage >= 0 and percentage <= 100),
  submitted_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists nclex_attempts_group_idx
  on public.nclex_attempts (group_index, percentage desc, submitted_at asc);

create index if not exists nclex_attempts_student_group_idx
  on public.nclex_attempts (student_id, group_index);
