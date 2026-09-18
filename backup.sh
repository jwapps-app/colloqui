#!/bin/sh
# Colloqui backup: Postgres dump + uploads archive, with rotation.
# Run by the `backup` container daily; also runnable by hand.
#
# Each artifact is written to a temporary name and only renamed into place
# once it completed successfully, so a failed or interrupted dump can never
# masquerade as a good snapshot (and never counts toward retention). Any
# failure aborts before rotation, so a broken run can't rotate good snapshots
# out.
set -eu

DIR=/backups
KEEP=14
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DIR"

# Database — custom format (compressed, restorable with pg_restore)
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h db -U app -d app -Fc -f "$DIR/.db-$STAMP.dump.tmp"
mv "$DIR/.db-$STAMP.dump.tmp" "$DIR/db-$STAMP.dump"

# Uploaded files (avatars + attachments)
if [ -d /data/uploads ]; then
  tar czf "$DIR/.uploads-$STAMP.tar.gz.tmp" -C /data uploads
  mv "$DIR/.uploads-$STAMP.tar.gz.tmp" "$DIR/uploads-$STAMP.tar.gz"
fi

# Rotation: keep the most recent $KEEP of each kind (completed files only;
# in-progress .tmp files never match these patterns).
for pattern in 'db-*.dump' 'uploads-*.tar.gz'; do
  find "$DIR" -maxdepth 1 -name "$pattern" -type f | sort -r | tail -n +$((KEEP + 1)) \
    | while IFS= read -r old; do rm -f "$old"; done
done

echo "[backup] $STAMP complete ($(find "$DIR" -maxdepth 1 -name 'db-*.dump' | wc -l) db snapshots retained)"
