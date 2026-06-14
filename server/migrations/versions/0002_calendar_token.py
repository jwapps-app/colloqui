"""Add users.calendar_token for the private iCal feed.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("calendar_token", sa.String(64), nullable=True))
    op.create_index(
        "ix_users_calendar_token", "users", ["calendar_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_users_calendar_token", table_name="users")
    op.drop_column("users", "calendar_token")
