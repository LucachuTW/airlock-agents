"""Replace the synthetic sales demo with the real UCI Online Retail dataset.

UCI ML Repository #352, CC BY 4.0 — actual transactions of a UK-based online
retailer, Dec 2010 – Dec 2011 (~540k rows). Loaded by scripts/seed.py.

Revision ID: 0004
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL = """
drop table if exists sales.orders;
drop table if exists sales.customers;

create table sales.transactions (
  id           bigint generated always as identity primary key,
  invoice_no   text not null,
  stock_code   text not null,
  description  text,
  quantity     integer not null,
  invoice_date timestamptz not null,
  unit_price   numeric(12,4) not null,
  customer_id  integer,
  country      text not null
);
create index transactions_date_idx on sales.transactions (invoice_date);
create index transactions_country_idx on sales.transactions (country);
create index transactions_invoice_idx on sales.transactions (invoice_no);

-- Surfaced to the agent by mcp-db's list_tables: real-data semantics it must know.
comment on table sales.transactions is
  'UCI Online Retail: one row per invoice line of a UK online retailer (Dec 2010 - Dec 2011). '
  'Invoice numbers starting with C are cancellations and have negative quantity. '
  'Revenue of a line = quantity * unit_price. customer_id is NULL for guest checkouts.';

grant select on all tables in schema sales to mcp_readonly;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute("drop table if exists sales.transactions")
