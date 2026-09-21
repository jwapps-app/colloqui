"""Enforce channel-name uniqueness per space in the database.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-21

The API refused duplicate names with a read-then-insert check, which two
concurrent requests could both pass, and moving a channel between spaces
never checked at all. A partial unique index (non-DM channels only; DMs have
no name) makes the guarantee real. Semantics match the existing check
exactly: same space, same name, case-sensitive.

Self-healing: any exact duplicates that already exist are renamed with a
numbered suffix (oldest keeps the name) BEFORE the index is created, so this
migration cannot fail a deploy. IF NOT EXISTS keeps the index idempotent.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

INDEX = "ux_channels_space_name"


def upgrade() -> None:
    conn = op.get_bind()
    dups = conn.execute(sa.text(
        "SELECT space_id, name FROM channels "
        "WHERE is_dm = false AND name IS NOT NULL "
        "GROUP BY space_id, name HAVING count(*) > 1"
    )).fetchall()
    for space_id, name in dups:
        rows = conn.execute(sa.text(
            "SELECT id FROM channels WHERE space_id = :s AND name = :n AND is_dm = false "
            "ORDER BY created_at, id"
        ), {"s": space_id, "n": name}).fetchall()
        taken = {r[0] for r in conn.execute(sa.text(
            "SELECT name FROM channels WHERE space_id = :s AND is_dm = false"
        ), {"s": space_id}).fetchall()}
        n = 2
        for (cid,) in rows[1:]:  # the oldest keeps its name
            while True:
                suffix = f" ({n})"
                candidate = name[: 50 - len(suffix)] + suffix
                n += 1
                if candidate not in taken:
                    break
            taken.add(candidate)
            conn.execute(sa.text("UPDATE channels SET name = :c WHERE id = :id"),
                         {"c": candidate, "id": cid})
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON channels (space_id, name) "
        "WHERE is_dm = false"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
