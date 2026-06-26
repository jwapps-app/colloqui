"""Integration primitives: API keys and outgoing event subscriptions.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = insp.get_table_names()
    if "api_keys" not in tables:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column(
                "user_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
        op.create_index(
            "ix_api_keys_token_hash", "api_keys", ["token_hash"], unique=True
        )
    if "event_subscriptions" not in tables:
        op.create_table(
            "event_subscriptions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("url", sa.String(length=500), nullable=False),
            sa.Column("secret", sa.String(length=64), nullable=False),
            sa.Column("events", sa.String(length=300), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("event_subscriptions")
    op.drop_table("api_keys")
