"""Per-channel notification preferences.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "channel_notify_prefs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "channel_notify_prefs",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("channel_id", "user_id"),
    )
    op.create_index(
        "ix_channel_notify_prefs_user_id", "channel_notify_prefs", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_channel_notify_prefs_user_id", table_name="channel_notify_prefs")
    op.drop_table("channel_notify_prefs")
