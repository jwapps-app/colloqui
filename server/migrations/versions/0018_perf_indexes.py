"""Composite indexes for the per-channel count queries and reminder lookups.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-02
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


# The channel listing counts messages by (channel_id, deleted_at, created_at)
# and pinned messages by (channel_id, pinned_at); reminders are looked up by
# (user_id, fired_at). IF NOT EXISTS keeps this idempotent on re-runs.
INDEXES = [
    ("ix_messages_channel_deleted_created", "messages", "(channel_id, deleted_at, created_at)"),
    ("ix_messages_channel_pinned", "messages", "(channel_id, pinned_at)"),
    ("ix_reminders_user_fired", "reminders", "(user_id, fired_at)"),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}")


def downgrade() -> None:
    for name, _table, _cols in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
