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
    op.add_column("messages", sa.Column("thread_root_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_messages_thread_root", "messages", "messages",
        ["thread_root_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_messages_thread_root_id", "messages", ["thread_root_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_messages_thread_root_id", table_name="messages")
    op.drop_constraint("fk_messages_thread_root", "messages", type_="foreignkey")
    op.drop_column("messages", "thread_root_id")
