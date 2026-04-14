-- Supabase SQL: NCLEX question bank for admin CRUD + student practice page

create extension if not exists pgcrypto;

create table if not exists public.nclex_questions (
    id uuid primary key default gen_random_uuid(),
    question_type text not null check (question_type in ('mcq', 'sata', 'fill_blank', 'ordered_response')),
    question_text text not null,
    options jsonb not null default '[]'::jsonb,
    correct_answers jsonb not null default '[]'::jsonb,
    rationale text,
    difficulty text not null default 'medium' check (difficulty in ('easy', 'medium', 'hard')),
    display_order integer not null default 0,
    is_active boolean not null default true,
    created_by uuid,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_nclex_questions_active_order
    on public.nclex_questions (is_active, display_order, created_at desc);

create index if not exists idx_nclex_questions_type
    on public.nclex_questions (question_type);

create or replace function public.nclex_questions_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_nclex_questions_updated_at on public.nclex_questions;
create trigger trg_nclex_questions_updated_at
before update on public.nclex_questions
for each row
execute function public.nclex_questions_set_updated_at();
