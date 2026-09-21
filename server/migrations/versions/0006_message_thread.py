"""Add thread_root_id to messages (threaded replies).

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "thread_root_id" not in [c["name"] for c in insp.get_columns("messages")]:
        op.add_column("messages", sa.Column("thread_root_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "fk_messages_thread_root", "messages", "messages",
            ["thread_root_id"], ["id"], ondelete="SET NULL",
        )
    if "ix_messages_thread_root_id" not in [i["name"] for i in insp.get_indexes("messages")]:
        op.create_index(
            "ix_messages_thread_root_id", "messages", ["thread_root_id"]
        )


def downgrade() -> None:
    # Column drops take their index and FK with them, whatever the FK is
    # named (a fresh install's create_all auto-names it).
    op.execute("DROP INDEX IF EXISTS ix_messages_thread_root_id")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS thread_root_id")
