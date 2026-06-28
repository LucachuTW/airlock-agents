"""runs.route: which subagent the supervisor routed to (runs.agent = what to rebuild on resume).

Revision ID: 0003
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table runs add column route text")


def downgrade() -> None:
    op.execute("alter table runs drop column route")
