"""Recurring reminders: a recurrence interval on reminders.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("reminders")]
    if "recurrence" not in cols:
        op.add_column(
            "reminders", sa.Column("recurrence", sa.String(length=16), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("reminders", "recurrence")
