create table if not exists nodes (
  id text primary key,
  name text not null,
  url text not null unique,
  geo text not null default '',
  status text not null default 'unknown',
  capacity integer not null check (capacity > 0),
  api_key text,
  last_health_check timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists jobs (
  id text primary key,
  status text not null check (status in ('queued', 'running', 'success', 'failed')),
  count integer not null check (count > 0),
  product text not null,
  node_id text references nodes(id) on delete set null,
  start_port integer,
  profile jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  result_path text,
  error text
);

create index if not exists idx_jobs_status on jobs(status);
create index if not exists idx_jobs_node on jobs(node_id);

create table if not exists job_events (
  id bigserial primary key,
  job_id text not null references jobs(id) on delete cascade,
  event text not null,
  data jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_job_events_job_id on job_events(job_id);
