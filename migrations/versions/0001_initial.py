"""Initial schema — mirrors docs/DESIGN.md §3.

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
create extension if not exists vector;

create table users (
  id            uuid primary key default gen_random_uuid(),
  email         text unique not null,
  password_hash text not null,
  role          text not null check (role in ('admin','analyst','viewer')),
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);

create table tools (
  name        text primary key,
  mcp_server  text not null,
  description text not null,
  risk        text not null check (risk in ('read','write','critical')),
  enabled     boolean not null default true
);

create table role_tool_grants (
  role      text not null,
  tool_name text not null references tools(name),
  primary key (role, tool_name)
);

create table runs (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id),
  agent       text,
  status      text not null default 'running'
              check (status in ('running','waiting_approval','completed','failed','rejected')),
  input       text not null,
  output      text,
  trace_id    text,
  model       text,
  tokens_in   integer default 0,
  tokens_out  integer default 0,
  latency_ms  integer,
  created_at  timestamptz not null default now(),
  finished_at timestamptz
);
create index runs_user_created_idx on runs (user_id, created_at desc);
create index runs_waiting_idx on runs (status) where status = 'waiting_approval';

create table approvals (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references runs(id),
  tool_name     text not null references tools(name),
  payload       jsonb not null,
  status        text not null default 'pending'
                check (status in ('pending','approved','rejected','expired')),
  decided_by    uuid references users(id),
  decision_note text,
  expires_at    timestamptz not null,
  decided_at    timestamptz,
  created_at    timestamptz not null default now()
);
create index approvals_pending_idx on approvals (status) where status = 'pending';

create table audit_log (
  id         bigint generated always as identity primary key,
  actor_id   uuid,
  run_id     uuid,
  action     text not null,
  resource   text,
  detail     jsonb,
  trace_id   text,
  created_at timestamptz not null default now()
);
create index audit_log_run_idx on audit_log (run_id);
create index audit_log_created_idx on audit_log (created_at);

-- Append-only enforcement: even the table owner cannot mutate history.
create function audit_log_immutable() returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only';
end $$;
create trigger audit_log_no_mutation
  before update or delete on audit_log
  for each statement execute function audit_log_immutable();

create table documents (
  id         uuid primary key default gen_random_uuid(),
  title      text not null,
  source_uri text not null,
  version    integer not null default 1,
  created_at timestamptz not null default now()
);

create table chunks (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  ord         integer not null,
  content     text not null,
  embedding   vector(768) not null
);
create index chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);

create table tickets (
  id          uuid primary key default gen_random_uuid(),
  approval_id uuid not null unique references approvals(id),
  title       text not null,
  body        text not null,
  priority    text not null,
  created_at  timestamptz not null default now()
);

create table eval_cases (
  id       uuid primary key default gen_random_uuid(),
  suite    text not null,
  input    text not null,
  expected jsonb not null,
  metadata jsonb
);

create table eval_runs (
  id         uuid primary key default gen_random_uuid(),
  suite      text not null,
  kind       text not null check (kind in ('offline','online')),
  git_sha    text,
  model      text,
  created_at timestamptz not null default now()
);

create table eval_results (
  id          uuid primary key default gen_random_uuid(),
  eval_run_id uuid not null references eval_runs(id),
  case_id     uuid,
  run_id      uuid references runs(id),
  scores      jsonb not null,
  passed      boolean not null,
  tokens      integer,
  latency_ms  integer
);
create unique index eval_results_unique_idx
  on eval_results (eval_run_id, coalesce(case_id, run_id));

create table feedback (
  id         uuid primary key default gen_random_uuid(),
  run_id     uuid not null references runs(id),
  user_id    uuid not null references users(id),
  rating     smallint not null check (rating in (-1, 1)),
  comment    text,
  created_at timestamptz not null default now(),
  unique (run_id, user_id)
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(
        """
        drop table if exists feedback, eval_results, eval_runs, eval_cases, tickets,
          chunks, documents, audit_log, approvals, runs, role_tool_grants, tools, users cascade;
        drop function if exists audit_log_immutable() cascade;
        """
    )
