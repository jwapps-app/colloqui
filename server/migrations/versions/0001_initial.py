"""Baseline schema.

Builds the full current schema from the ORM models. Using create_all keeps
this baseline exactly in sync with the models and makes it idempotent: on the
existing production database (whose tables were created by the pre-Alembic
startup) it creates nothing and simply records the version, while a fresh
deploy gets the complete schema. Subsequent migrations are authored normally.

Revision ID: 0001
Revises:
Create Date: 2026-06-13
"""
from alembic import op

from app import models  # noqa: F401 — registers tables on Base.metadata
from app.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
