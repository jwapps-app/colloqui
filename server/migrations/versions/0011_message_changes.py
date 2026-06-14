"""Change-log + sequence for offline sync.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The sequence is app-driven (not created by create_all), so make it here
    # on both fresh and existing databases.
    op.execute("CREATE SEQUENCE IF NOT EXISTS change_seq")
    if "message_changes" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "message_changes",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_message_changes_seq", "message_changes", ["seq"])
    op.create_index("ix_message_changes_channel_id", "message_changes", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_message_changes_channel_id", table_name="message_changes")
    op.drop_index("ix_message_changes_seq", table_name="message_changes")
    op.drop_table("message_changes")
    op.execute("DROP SEQUENCE IF EXISTS change_seq")
