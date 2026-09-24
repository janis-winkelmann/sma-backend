create table if not exists public.tiktok_users (
  id bigint generated always as identity primary key,
  username text not null unique,
  name text,
  bio text,
  pfp text,
  sec_uid text unique,
  visibility text,
  last_scraped timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.tiktok_posts (
  id bigint generated always as identity primary key,
  post_id text not null unique,
  sec_uid text not null,
  is_deleted boolean not null default false,
  type text not null check (type in ('video', 'live', 'story', 'images')),
  caption text,
  chunks jsonb not null default '[]'::jsonb,
  thumbnail text,
  posted_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.tiktok_posts add column if not exists thumbnail text;

create index if not exists tiktok_users_last_scraped_idx
  on public.tiktok_users (last_scraped);

create index if not exists tiktok_posts_sec_uid_idx
  on public.tiktok_posts (sec_uid);

grant select, insert, update, delete on public.tiktok_users, public.tiktok_posts to service_role;
grant usage, select on all sequences in schema public to service_role;

alter table public.tiktok_users enable row level security;
alter table public.tiktok_posts enable row level security;
