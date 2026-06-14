"""Incoming webhooks + Message.webhook_name.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("webhook_name", sa.String(length=64), nullable=True))
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhooks_channel_id", "webhooks", ["channel_id"])
    op.create_index("ix_webhooks_token_hash", "webhooks", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_webhooks_token_hash", table_name="webhooks")
    op.drop_index("ix_webhooks_channel_id", table_name="webhooks")
    op.drop_table("webhooks")
    op.drop_column("messages", "webhook_name")
