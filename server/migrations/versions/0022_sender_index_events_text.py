"""Index messages.sender_id and widen event_subscriptions.events.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-01

- messages.sender_id had no index; the threads inbox filters by it twice
  (roots the user replied in, roots the user started), a full scan that grows
  with total server volume rather than the user's own message count.
- event_subscriptions.events was String(300) but the request schema admits
  up to 20 event names, which can exceed 300 chars joined and failed with a
  500 at flush. Text removes the mismatch.
IF NOT EXISTS keeps the index step idempotent on re-runs.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_messages_sender_id ON messages (sender_id)")
    op.alter_column(
        "event_subscriptions",
        "events",
        type_=sa.Text(),
        existing_type=sa.String(300),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "event_subscriptions",
        "events",
        type_=sa.String(300),
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.execute("DROP INDEX IF EXISTS ix_messages_sender_id")
