import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

USERNAME_PATTERN = r"^[A-Za-z0-9_]{3,32}$"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    is_admin: bool
    avatar_at: datetime | None = None


class RegisterOptionsIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    invite_code: str | None = Field(default=None, max_length=64)


class LoginOptionsIn(BaseModel):
    username: str | None = Field(default=None, max_length=32)


class LoginPasswordIn(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=200)


class RegisterPasswordIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    invite_code: str | None = Field(default=None, max_length=64)
    password: str = Field(min_length=8, max_length=200)


class SetPasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    current_password: str | None = Field(default=None, max_length=200)


class VerifyIn(BaseModel):
    token: str = Field(max_length=64)
    credential: dict


class TokenOut(BaseModel):
    token: str
    user: UserOut


class UpdateMeIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class SpaceOut(BaseModel):
    id: uuid.UUID
    name: str
    is_default: bool
    my_role: str | None = None  # manager | member | None (server admin, non-member)


class SpaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class SpaceMemberIn(BaseModel):
    user_id: uuid.UUID
    role: str = Field(default="member", pattern=r"^(member|manager)$")


class SpaceMemberRoleIn(BaseModel):
    role: str = Field(pattern=r"^(member|manager)$")


class SpaceMemberOut(UserOut):
    role: str


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    is_private: bool = False
    space_id: uuid.UUID


class ChannelUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    topic: str | None = Field(default=None, max_length=200)


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str | None
    topic: str | None = None
    is_private: bool
    is_dm: bool
    space_id: uuid.UUID | None = None
    dm_user: UserOut | None = None  # the other person in a 1:1 DM
    dm_members: list[UserOut] = []  # the other people in a group DM (3+)
    my_role: str | None = None  # member | owner | None (admin viewing)
    message_count: int = 0  # all-time total
    recent_count: int = 0   # messages in the last 7 days (current activity)
    unread_count: int = 0
    open_task_count: int = 0
    reminder_count: int = 0
    pinned_count: int = 0
    thread_count: int = 0  # active threads (recent activity)
    notify_level: str = "all"  # all | mentions | muted


class MemberIn(BaseModel):
    user_id: uuid.UUID


class NotifyPrefIn(BaseModel):
    level: str = Field(pattern=r"^(all|mentions|muted)$")


class DeviceRegisterIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    platform: str = Field(default="ios", max_length=16)


class WebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


class WebhookCreatedOut(WebhookOut):
    url: str  # full ingest URL with the secret token — shown exactly once


class WebhookPostIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, max_length=64)  # overrides the default name


class DMIn(BaseModel):
    # one user → 1:1 DM; multiple → group DM
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int


class ReplyPreview(BaseModel):
    id: uuid.UUID
    sender_name: str
    snippet: str


class MessageIn(BaseModel):
    content: str = Field(default="", max_length=4000)
    file_id: uuid.UUID | None = None
    reply_to_id: uuid.UUID | None = None
    thread_root_id: uuid.UUID | None = None
    # Client-supplied id for idempotent offline send: replaying the same id
    # returns the existing message instead of creating a duplicate.
    id: uuid.UUID | None = None


class ReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


class CheckboxIn(BaseModel):
    line: int = Field(ge=0, le=500)
    checked: bool


class ReactionAgg(BaseModel):
    emoji: str
    count: int
    user_ids: list[uuid.UUID]


class LinkPreviewOut(BaseModel):
    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    channel_id: uuid.UUID
    sender: UserOut
    content: str
    created_at: datetime
    edited_at: datetime | None = None
    file: FileOut | None = None
    reactions: list[ReactionAgg] = []
    task_cleared: dict[str, datetime] = {}  # line index (str) -> when checked
    reply_to: ReplyPreview | None = None
    pinned: bool = False
    link_previews: list[LinkPreviewOut] = []
    deleted_at: datetime | None = None  # set only in the sync feed, for tombstones
    thread_root_id: uuid.UUID | None = None
    reply_count: int = 0  # replies in this message's thread (roots only)
    thread_last_at: datetime | None = None
    thread_repliers: list[UserOut] = []  # a few distinct repliers, for avatars


class InviteIn(BaseModel):
    expires_hours: int = Field(default=72, ge=1, le=720)
    # Username of an existing account to recover instead of creating a new one
    recover_username: str | None = Field(default=None, max_length=32)


class InviteCreatedOut(BaseModel):
    id: uuid.UUID
    code: str  # plaintext, shown exactly once
    expires_at: datetime
    recovery_for: str | None = None


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    used_by: uuid.UUID | None
    used_at: datetime | None
    recover_user_id: uuid.UUID | None


class PasskeyOut(BaseModel):
    id: str  # base64url credential id
    label: str | None
    created_at: datetime
    last_used_at: datetime | None


class AddPasskeyIn(BaseModel):
    label: str | None = Field(default=None, max_length=64)


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    current: bool


class SearchHit(BaseModel):
    channel_id: uuid.UUID
    channel_name: str | None
    is_dm: bool
    message: MessageOut


class OpenTaskOut(BaseModel):
    message_id: uuid.UUID
    channel_id: uuid.UUID
    channel_name: str | None
    is_dm: bool
    line: int
    text: str
    created_at: datetime


class ThreadDigestOut(BaseModel):
    channel_id: uuid.UUID
    channel_name: str | None
    is_dm: bool
    root: MessageOut  # carries reply_count, thread_last_at, thread_repliers


class SyncOut(BaseModel):
    """Delta of message changes since `cursor`. Deleted messages come through
    with `deleted_at` set (tombstones). Call again with the returned `cursor`
    while `has_more` is true."""

    messages: list[MessageOut]
    cursor: int
    has_more: bool


class AdminUserOut(UserOut):
    disabled: bool
    created_at: datetime
    pending: bool = False  # no passkey AND no password enrolled yet
    has_password: bool = False


class AdminCreateUserIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    is_admin: bool = False
    password: str | None = Field(default=None, min_length=8, max_length=200)


class AdminUserCreatedOut(BaseModel):
    username: str
    display_name: str
    claim_code: str  # shown once; the person enrolls their passkey with it
    expires_at: datetime


class AdminUserUpdateIn(BaseModel):
    disabled: bool | None = None
    is_admin: bool | None = None


class AdminChannelOut(BaseModel):
    id: uuid.UUID
    name: str | None
    is_private: bool
    created_at: datetime
    member_count: int


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    due_at: datetime
    channel_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    due_at: datetime
    channel_id: uuid.UUID | None
    message_id: uuid.UUID | None
    created_at: datetime
    fired_at: datetime | None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str
    data: dict | None
    created_at: datetime
    read_at: datetime | None
