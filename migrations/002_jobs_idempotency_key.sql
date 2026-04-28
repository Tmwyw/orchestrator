alter table jobs
  add column if not exists idempotency_key text;

create unique index if not exists idx_jobs_idempotency_key
  on jobs(idempotency_key);
