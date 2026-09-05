"""Browser-only mailbox credentials and OAuth state.
Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("connection_links",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("state_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("inbox_id", sa.String(40), sa.ForeignKey("inboxes.id")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("connection_links")
