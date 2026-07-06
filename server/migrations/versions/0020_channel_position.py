"""Manual sort order (position) for channels within a space.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("channels")]
    if "position" in cols:
        return
    op.add_column(
        "channels",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("channels", "position", server_default=None)
    # Seed positions from the current per-space creation order so the existing
    # sidebar arrangement is preserved as the starting point.
    op.execute(
        """
        UPDATE channels SET position = sub.rn FROM (
            SELECT id, (row_number() OVER (
                PARTITION BY space_id ORDER BY created_at
            )) - 1 AS rn
            FROM channels WHERE is_dm = false
        ) sub WHERE channels.id = sub.id
        """
    )


def downgrade() -> None:
    op.drop_column("channels", "position")
