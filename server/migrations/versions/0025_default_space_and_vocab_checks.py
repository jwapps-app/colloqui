"""Move the default-space backfill out of startup; enforce the closed value sets.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-25

Three things that used to be handled outside the schema:

1. Pre-spaces data. Servers created before spaces existed had their users and
   channels moved into a default "Main" space by a function that ran at EVERY
   startup, long after it had done its one job. That step lives here now and
   the startup code is gone. It only acts when no space exists but users do;
   on any current database it does nothing.

2. Closed value sets. Member roles, notification levels and reminder
   recurrence are small fixed vocabularies (app/vocab.py) that the database
   stored as free text. CHECK constraints now refuse anything else.

3. spaces.created_by was NOT NULL with ON DELETE SET NULL, so deleting a
   space's creator could only fail. The column is now nullable, which is what
   the foreign key always intended.

Cannot fail a deploy:
- Every step is idempotent. A fresh install's 0001 builds the full current
  schema from the models, so the constraints may already exist.
- Each CHECK is added NOT VALID (it applies to new and changed rows at once)
  and is then validated only if no existing row breaks it. An unexpected old
  value is left in place and reported instead of stopping the server.
"""
import logging
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# name -> (table, condition). Written out literally on purpose: a migration
# records the schema as of its own date and must not change when vocab.py does.
CHECKS = {
    "ck_space_members_role": ("space_members", "role IN ('member', 'manager')"),
    "ck_channel_members_role": ("channel_members", "role IN ('member', 'owner')"),
    "ck_channel_notify_prefs_level": (
        "channel_notify_prefs",
        "level IN ('all', 'mentions', 'muted')",
    ),
    "ck_reminders_recurrence": (
        "reminders",
        "recurrence IS NULL OR recurrence IN ('daily', 'weekly', 'monthly', 'yearly')",
    ),
}


def _backfill_default_space(conn) -> None:
    if conn.execute(sa.text("SELECT 1 FROM spaces LIMIT 1")).first() is not None:
        return
    users = conn.execute(
        sa.text("SELECT id, is_admin FROM users ORDER BY created_at")
    ).fetchall()
    if not users:
        return  # fresh server; the first registration creates the space
    space_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO spaces (id, name, is_default, position, created_by, created_at) "
            "VALUES (:id, 'Main', true, 0, :creator, now())"
        ),
        {"id": space_id, "creator": users[0][0]},
    )
    for user_id, is_admin in users:
        conn.execute(
            sa.text(
                "INSERT INTO space_members (space_id, user_id, role, joined_at) "
                "VALUES (:s, :u, :r, now())"
            ),
            {"s": space_id, "u": user_id, "r": "manager" if is_admin else "member"},
        )
    conn.execute(
        sa.text(
            "UPDATE channels SET space_id = :s WHERE is_dm = false AND space_id IS NULL"
        ),
        {"s": space_id},
    )


def _add_checks(conn) -> None:
    existing = {
        row[0]
        for row in conn.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE contype = 'c'")
        )
    }
    for name, (table, condition) in CHECKS.items():
        if name not in existing:
            conn.execute(
                sa.text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID"
                )
            )
        validated = conn.execute(
            sa.text("SELECT convalidated FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
        if validated:
            continue
        offenders = conn.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")
        ).scalar()
        if offenders:
            log.warning(
                "%s: %s existing row(s) in %s do not satisfy (%s). The constraint "
                "is enforced for new and changed rows only; fix those rows, then "
                "run: ALTER TABLE %s VALIDATE CONSTRAINT %s",
                name, offenders, table, condition, table, name,
            )
        else:
            conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def upgrade() -> None:
    conn = op.get_bind()
    # Before the backfill: it may need to insert a space, and the column's old
    # NOT NULL is exactly what this migration removes.
    op.execute("ALTER TABLE spaces ALTER COLUMN created_by DROP NOT NULL")
    _backfill_default_space(conn)
    _add_checks(conn)


def downgrade() -> None:
    for name, (table, _) in CHECKS.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    # spaces.created_by stays nullable: restoring NOT NULL would fail on any
    # space whose creator has since been deleted, and nothing depends on it.
    # The default-space backfill is data, not schema, and is not undone.
