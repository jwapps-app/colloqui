"""Web Push (VAPID) subscriptions for PWA notifications.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "push_subscriptions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("p256dh", sa.String(length=200), nullable=False),
        sa.Column("auth", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("endpoint"),
    )
    op.create_index(
        "ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_push_subscriptions_user_id", table_name="push_subscriptions"
    )
    op.drop_table("push_subscriptions")
