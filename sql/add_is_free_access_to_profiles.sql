-- Run in Supabase: SQL Editor → New query → Run.
-- Fixes PostgREST PGRST204 when the app uses profiles.is_free_access.

alter table public.profiles
  add column if not exists is_free_access boolean not null default false;

comment on column public.profiles.is_free_access is
  'When true, the student bypasses subscription payment gates (admin-granted free access).';
