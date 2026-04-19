-- Scheduled site maintenance (Django reads via service role).
-- Run in Supabase → SQL Editor.
--
-- ends_at NULL = no automatic end; stays active until you delete the row or set ends_at / disable.
-- Lock: website.middleware.MaintenanceModeMiddleware redirects non-admins to /maintenance/
--       except /login/, /logout/, /auth/*, password reset, /admin-panel/*, assets, /maintenance/.

create table if not exists public.site_maintenance (
  id uuid primary key default gen_random_uuid(),
  title text not null default 'Scheduled maintenance',
  message text not null default '',
  image_url text,
  starts_at timestamptz not null,
  ends_at timestamptz,
  is_enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.site_maintenance is 'When enabled and within the window, non-admin users see /maintenance/ and APIs return 503.';
comment on column public.site_maintenance.ends_at is 'Optional. NULL means open-ended until deleted or disabled.';

create index if not exists idx_site_maintenance_enabled_starts
  on public.site_maintenance (is_enabled, starts_at desc);

alter table public.site_maintenance enable row level security;

-- Browser/anon: no policies → no direct access. Django uses service role (bypasses RLS).
