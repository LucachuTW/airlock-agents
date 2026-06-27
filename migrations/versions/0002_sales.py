"""Demo analytics schema `sales` + read-only DB role for the mcp-db server.

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL = """
create schema if not exists sales;

create table sales.customers (
  id         serial primary key,
  name       text not null,
  country    text not null,
  segment    text not null check (segment in ('smb','mid','enterprise')),
  created_at date not null
);

create table sales.orders (
  id          serial primary key,
  customer_id integer not null references sales.customers(id),
  order_date  date not null,
  status      text not null check (status in ('pending','paid','cancelled','refunded')),
  total_eur   numeric(10,2) not null
);
create index orders_date_idx on sales.orders (order_date);

-- The mcp-db server connects as this role: SELECT on `sales` only, nothing else
-- (no access to users, runs, audit_log, ...). Defense in depth below sqlglot validation.
do $$ begin
  if not exists (select from pg_roles where rolname = 'mcp_readonly') then
    create role mcp_readonly login password 'mcp_readonly';
  end if;
end $$;
grant usage on schema sales to mcp_readonly;
grant select on all tables in schema sales to mcp_readonly;
alter default privileges in schema sales grant select on tables to mcp_readonly;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(
        """
        drop schema if exists sales cascade;
        drop owned by mcp_readonly;
        drop role if exists mcp_readonly;
        """
    )
