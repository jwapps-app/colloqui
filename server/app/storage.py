"""Where uploaded bytes live on disk. The ONLY place a path under the upload
directory is built: blobs are stored under our own UUIDs (client filenames
never touch a path), avatars under the user's id, plus the server's own
generated key file."""
import uuid
from collections.abc import Iterable
from pathlib import Path

from .config import settings


def upload_root() -> Path:
    # Read at call time: tests (and a changed setting) repoint it.
    return Path(settings.upload_dir)


def blob_path(file_id: uuid.UUID) -> Path:
    return upload_root() / str(file_id)


def avatar_path(user_id: uuid.UUID) -> Path:
    return upload_root() / "avatars" / str(user_id)


def vapid_key_path() -> Path:
    return upload_root() / "vapid.json"


def unlink_blobs(file_ids: Iterable[uuid.UUID]) -> None:
    """Delete file blobs. Queue this with db.after_commit: removing a blob
    before its row's deletion commits leaves rows pointing at nothing if the
    commit fails."""
    for file_id in file_ids:
        blob_path(file_id).unlink(missing_ok=True)


def unlink_avatar(user_id: uuid.UUID) -> None:
    avatar_path(user_id).unlink(missing_ok=True)
