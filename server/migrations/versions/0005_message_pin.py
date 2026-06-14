"""Add pinned_at/pinned_by to messages.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "pinned_at" not in [c["name"] for c in insp.get_columns("messages")]:
        op.add_column("messages", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("messages", sa.Column("pinned_by", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "fk_messages_pinned_by", "messages", "users",
            ["pinned_by"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint("fk_messages_pinned_by", "messages", type_="foreignkey")
    op.drop_column("messages", "pinned_by")
    op.drop_column("messages", "pinned_at")
