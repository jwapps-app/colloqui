"""The shared vocabulary: every closed set of values the server, the database
and the web client all have to agree on. Defined here once. Schemas validate
against these, handlers compare against these, and the client reads them from
GET /api/v1/meta instead of hardcoding its own copy."""
import re
from enum import StrEnum


class ChannelRole(StrEnum):
    MEMBER = "member"
    OWNER = "owner"


class SpaceRole(StrEnum):
    MEMBER = "member"
    MANAGER = "manager"


class NotifyLevel(StrEnum):
    """Per-user, per-channel notification level."""

    ALL = "all"            # every message
    MENTIONS = "mentions"  # only @mentions and threads you're in
    MUTED = "muted"        # nothing; hidden from unread


# What members hear before they pick a per-channel preference. No row is stored
# for the default, so a missing preference means this.
DEFAULT_NOTIFY_LEVEL = NotifyLevel.ALL


class Recurrence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


# The reactions a message can carry, in display order.
ALLOWED_EMOJI = (
    "👍", "👎", "❤️", "🔥", "🎉", "😂", "😍", "🤔", "😮", "😢",
    "😡", "🙏", "👏", "✅", "❌", "⭐", "💯", "👀", "🚀", "✨",
    "🙌", "💪", "🤝", "👋", "🤣", "😅", "😊", "😎", "🥳", "😤",
    "😴", "🤯", "🤷", "🍕", "☕", "💔", "⚡", "📌", "➕", "🎯",
)

# Usernames: what registration accepts, and what an @mention can name.
USERNAME_PATTERN = r"^[A-Za-z0-9_]{3,32}$"
_MENTION_RE = re.compile(r"@([a-z0-9_]{3,32})")


def mentioned_usernames(content: str) -> set[str]:
    """Lowercased usernames @mentioned in a message body. Usernames are stored
    lowercase, so compare with `username.lower() in ...`."""
    return set(_MENTION_RE.findall(content.lower()))
