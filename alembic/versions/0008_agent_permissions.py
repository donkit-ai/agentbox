"""Inbox-scoped agent keys and attachment ownership.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("inbox_ids", postgresql.JSONB()))
    op.add_column("api_keys", sa.Column("permission_profile", sa.String(30)))
    op.add_column("api_keys", sa.Column("allowed_recipients", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("attachments", sa.Column("uploaded_by_key_id", sa.String(40)))
    op.execute("UPDATE domains SET organization_id = NULL WHERE type = 'customer_external'")


def downgrade() -> None:
    op.drop_column("attachments", "uploaded_by_key_id")
    op.drop_column("api_keys", "allowed_recipients")
    op.drop_column("api_keys", "permission_profile")
    op.drop_column("api_keys", "inbox_ids")
