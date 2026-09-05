"""Authenticated inbound provider webhooks and durable retry deduplication.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_endpoints",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inbound_endpoints_organization_id", "inbound_endpoints", ["organization_id"])
    op.add_column("inbound_ingests", sa.Column("dedupe_key", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_inbound_ingests_dedupe_key", "inbound_ingests", ["dedupe_key"])


def downgrade() -> None:
    op.drop_constraint("uq_inbound_ingests_dedupe_key", "inbound_ingests", type_="unique")
    op.drop_column("inbound_ingests", "dedupe_key")
    op.drop_table("inbound_endpoints")
