import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# A thread is "active" while it has had a reply within this many days; after
# that it drops out of the threads lists (it stays reachable, and a new reply
# revives it).
THREAD_ACTIVE_DAYS = 7


class User(Base):
    """Identity only — knows nothing about how the user logs in."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    avatar_type: Mapped[str | None] = mapped_column(String(50))
    avatar_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Unguessable token for the private iCal calendar feed (capability URL).
    calendar_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)


class WebAuthnCredential(Base):
    """One login method for a user. Future methods (password, etc.) get their
    own tables; nothing outside auth ever touches credentials."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)  # credential ID
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordCredential(Base):
    """Optional password login — a second credential type alongside passkeys.
    One per user; Argon2id hash. Identity/sessions are untouched."""

    __tablename__ = "password_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Session(Base):
    """Opaque bearer tokens; only the SHA-256 hash is stored."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))


class Invite(Base):
    """Single-use invite codes; only the hash is stored (shown once at creation)."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When set, this invite re-enrolls an existing account (lost passkey
    # recovery) instead of creating a new user.
    recover_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )


class File(Base):
    """Upload metadata; the blob lives on disk named by this row's id."""

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    uploader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(128))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Space(Base):
    """A walled-off group: its own members and channels. Users see only the
    spaces they belong to. DMs are cross-space and have no space."""

    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpaceMember(Base):
    __tablename__ = "space_members"

    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")  # member | manager
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(50))  # null for DMs
    topic: Mapped[str | None] = mapped_column(String(200))
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    # null for DMs; required for regular channels
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    is_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelMember(Base):
    __tablename__ = "channel_members"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")  # member | owner
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkPreview(Base):
    """Cached text metadata for a URL (fetched once, server-side). Keyed by a
    hash of the URL; `ok` is False for URLs that yielded nothing usable, so we
    don't refetch them every time the message renders."""

    __tablename__ = "link_previews"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(300))
    site_name: Mapped[str | None] = mapped_column(String(100))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Webhook(Base):
    """An incoming webhook: a secret URL an external system POSTs to, which
    appears as a message in `channel_id` under the webhook's `name`."""

    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChannelNotifyPref(Base):
    """Per-user, per-channel notification level. Absence of a row means the
    default: 'all' for DMs, 'mentions' for channels."""

    __tablename__ = "channel_notify_prefs"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    level: Mapped[str] = mapped_column(String(16))  # all | mentions | muted


class Reaction(Base):
    __tablename__ = "reactions"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    emoji: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskCompletion(Base):
    """When a `[x]` task line was checked. Keyed by message + line index;
    deleted when the box is unchecked, cleared wholesale when a message is
    edited (line numbers shift)."""

    __tablename__ = "task_completions"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    line: Mapped[int] = mapped_column(Integer, primary_key=True)
    cleared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(String(500))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    """Generic notification inbox. Today delivered via WebSocket (+ browser
    notifications); the future iOS APNs push hooks into the same dispatch
    point in notify.notify_user()."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))  # reminder | (future: mention, dm…)
    title: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceToken(Base):
    """An APNs device token for a user's iOS install. One token belongs to one
    user at a time; re-registering reassigns it. Dead tokens (APNs 410) are
    pruned on send."""

    __tablename__ = "device_tokens"

    token: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(16), default="ios")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageChange(Base):
    """Change-log for offline sync: one row per message that has been created,
    edited, reacted to, pinned, checked, or deleted since the feature shipped.
    `seq` is a monotonic cursor (from the `change_seq` sequence); a client polls
    `GET /sync?since=<seq>` to pull just what changed. One row per message
    (upserted) keeps the table bounded; the client fetches a message's current
    state, so only the latest change matters."""

    __tablename__ = "message_changes"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    # Bind the sequence to the metadata so create_all (fresh installs + tests)
    # creates it; the migration also makes it for existing DBs.
    seq: Mapped[int] = mapped_column(BigInteger, Sequence("change_seq"), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelRead(Base):
    """Per-user read marker for a channel. Unread = messages after this."""

    __tablename__ = "channel_reads"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL")
    )
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    # Set when the message was posted by an incoming webhook; carries the
    # webhook's display name so it shows as e.g. "Backups" rather than a user.
    webhook_name: Mapped[str | None] = mapped_column(String(64))
    # Root of a thread: NULL for top-level messages, else the message this
    # reply hangs under. Threads are flattened to one level (a reply to a
    # reply joins the same root), so the channel timeline shows only roots.
    thread_root_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pinned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
