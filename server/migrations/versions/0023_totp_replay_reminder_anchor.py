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


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent: a fresh install's 0001 creates the full current schema, so
    # these columns may already exist (every add_column migration here must
    # tolerate that or fresh deployments fail).
    if "last_used_step" not in _cols("totp_credentials"):
        op.add_column(
            "totp_credentials",
            sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        )
    if "anchor_at" not in _cols("reminders"):
        op.add_column(
            "reminders",
            sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute("UPDATE reminders SET anchor_at = due_at "
               "WHERE recurrence IS NOT NULL AND anchor_at IS NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS anchor_at")
    op.execute("ALTER TABLE totp_credentials DROP COLUMN IF EXISTS last_used_step")
