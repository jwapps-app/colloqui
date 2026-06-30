"""Per-token APNs environment (sandbox vs production) for device_tokens.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-30
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("device_tokens")}
    if "environment" not in cols:
        op.add_column(
            "device_tokens",
            sa.Column(
                "environment",
                sa.String(length=16),
                nullable=False,
                server_default="production",
            ),
        )


def downgrade() -> None:
    op.drop_column("device_tokens", "environment")
