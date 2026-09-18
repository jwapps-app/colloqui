"""TOTP replay protection and a stable recurrence anchor for reminders.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-18

- totp_credentials.last_used_step: the last accepted TOTP time-step, so the
  same code can't be replayed within its acceptance window.
- reminders.anchor_at: the original due time a recurring reminder counts from.
  Rolling the clamped date forward each month drifted (Jan 31 -> Feb 28 ->
  Mar 28); counting every occurrence from the anchor keeps the intended day.
  Backfilled from due_at for existing recurring reminders.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "totp_credentials",
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE reminders SET anchor_at = due_at WHERE recurrence IS NOT NULL")


def downgrade() -> None:
    op.drop_column("reminders", "anchor_at")
    op.drop_column("totp_credentials", "last_used_step")
