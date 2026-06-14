"""Add messages.reply_to_id for quote-replies.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("reply_to_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_messages_reply_to", "messages", "messages",
        ["reply_to_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_messages_reply_to", "messages", type_="foreignkey")
    op.drop_column("messages", "reply_to_id")
