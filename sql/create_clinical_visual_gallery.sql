-- Clinical Visual Library: image URLs shown to students at /dashboard/clinical-visuals/
-- Run this in Supabase → SQL Editor → Run.

create table if not exists public.clinical_visual_gallery (
  id uuid primary key default gen_random_uuid(),
  image_url text not null,
  caption text not null default '',
  sort_order integer not null default 0,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  created_by uuid null
);

comment on table public.clinical_visual_gallery is 'Admin-managed image URLs for the student Clinical Visual Library.';

create index if not exists idx_clinical_visual_gallery_sort
  on public.clinical_visual_gallery (sort_order asc, created_at desc);

create index if not exists idx_clinical_visual_gallery_active
  on public.clinical_visual_gallery (is_active)
  where is_active = true;

alter table public.clinical_visual_gallery enable row level security;

-- Django uses the service role key and bypasses RLS. If you ever query from the browser with the anon key:
create policy "clinical_visual_gallery_select_active"
  on public.clinical_visual_gallery
  for select
  using (is_active = true);

-- Optional: deny direct inserts from anon (admins use service role from Django only).
-- No insert/update/delete policies for anon → blocked for anon key.
