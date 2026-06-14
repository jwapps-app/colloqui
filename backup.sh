#!/bin/sh
# Colloqui backup: Postgres dump + uploads archive, with rotation.
# Run by the `backup` container daily; also runnable by hand.
set -eu

DIR=/backups
KEEP=14
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DIR"

# Database — custom format (compressed, restorable with pg_restore)
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h db -U app -d app -Fc -f "$DIR/db-$STAMP.dump"

# Uploaded files (avatars + attachments)
if [ -d /data/uploads ]; then
  tar czf "$DIR/uploads-$STAMP.tar.gz" -C /data uploads
fi

# Rotation: keep the most recent $KEEP of each kind
for pattern in 'db-*.dump' 'uploads-*.tar.gz'; do
  find "$DIR" -maxdepth 1 -name "$pattern" -type f | sort -r | tail -n +$((KEEP + 1)) \
    | while IFS= read -r old; do rm -f "$old"; done
done

echo "[backup] $STAMP complete ($(find "$DIR" -maxdepth 1 -name 'db-*.dump' | wc -l) db snapshots retained)"
