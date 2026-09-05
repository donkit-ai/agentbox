"""Durable reply waits and leased event consumers.
Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("reply_waits",
        sa.Column("message_id", sa.String(40), sa.ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("organization_id", sa.String(40), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("inbox_id", sa.String(40), sa.ForeignKey("inboxes.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reply_message_id", sa.String(40), sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("event_receipts",
        sa.Column("organization_id", sa.String(40), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("consumer", sa.String(100), primary_key=True),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("api_key_id", sa.String(40), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_table("event_receipts")
    op.drop_table("reply_waits")
