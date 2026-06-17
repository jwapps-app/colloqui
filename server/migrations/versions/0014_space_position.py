"""Manual sort order (position) for spaces.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("spaces")]
    if "position" in cols:
        return
    op.add_column(
        "spaces",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("spaces", "position", server_default=None)
    # Seed positions from the current order (default first, then name) so the
    # existing sidebar order is preserved as the initial arrangement.
    op.execute(
        """
        UPDATE spaces SET position = sub.rn FROM (
            SELECT id, (row_number() OVER (ORDER BY is_default DESC, name)) - 1 AS rn
            FROM spaces
        ) sub WHERE spaces.id = sub.id
        """
    )


def downgrade() -> None:
    op.drop_column("spaces", "position")
