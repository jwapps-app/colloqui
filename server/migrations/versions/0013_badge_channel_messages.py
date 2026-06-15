"""Per-user toggle: count ordinary channel messages toward the badge.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")]
    if "badge_channel_messages" in cols:
        return
    op.add_column(
        "users",
        sa.Column(
            "badge_channel_messages",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # Drop the server default now that existing rows are backfilled; the ORM
    # supplies the default on insert.
    op.alter_column("users", "badge_channel_messages", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "badge_channel_messages")
