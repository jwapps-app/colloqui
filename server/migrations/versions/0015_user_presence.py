"""Presence: custom status text and last-seen timestamp on users.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")]
    if "status" not in cols:
        op.add_column("users", sa.Column("status", sa.String(length=80), nullable=True))
    if "last_seen_at" not in cols:
        op.add_column(
            "users",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("users", "last_seen_at")
    op.drop_column("users", "status")
