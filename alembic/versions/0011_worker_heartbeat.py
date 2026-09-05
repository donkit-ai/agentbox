"""Worker liveness for actionable diagnostics.
Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("worker_heartbeats", sa.Column("worker_id", sa.String(200), primary_key=True),
        sa.Column("kinds", postgresql.JSONB(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
