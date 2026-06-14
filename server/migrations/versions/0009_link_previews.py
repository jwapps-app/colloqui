"""Cached link previews.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "link_previews" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "link_previews",
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("site_name", sa.String(length=100), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("url_hash"),
    )


def downgrade() -> None:
    op.drop_table("link_previews")
