-- Supabase SQL: store IELTS + NCLEX external book links

create extension if not exists pgcrypto;

create table if not exists public.resource_books (
    id uuid primary key default gen_random_uuid(),
    category text not null check (category in ('nclex', 'ielts')),
    title text not null,
    description text,
    external_url text not null,
    is_active boolean not null default true,
    created_by uuid,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_resource_books_category_active
    on public.resource_books (category, is_active, created_at desc);

create unique index if not exists uq_resource_books_category_title
    on public.resource_books (category, lower(trim(title)));

create unique index if not exists uq_resource_books_external_url
    on public.resource_books (lower(trim(external_url)));

create or replace function public.resource_books_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_resource_books_updated_at on public.resource_books;
create trigger trg_resource_books_updated_at
before update on public.resource_books
for each row
execute function public.resource_books_set_updated_at();
