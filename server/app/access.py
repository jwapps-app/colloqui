"""Who may do what. Every permission rule lives here, once; route handlers call
these and never spell a role or membership check inline.

Two shapes:
  require_*  load the object and raise the right HTTP error (404 where a 403
             would reveal that a private channel or space exists).
  is_* / can_*  plain predicates, for handlers that need their own error text
             or that combine the rule with something else.

The rules, in one place:
  * Channel member: has a ChannelMember row. Server admins get NO implicit
    read access to channels they are not in.
  * Channel manager: the channel's owner, or a server admin. An admin may
    rename, move and remove members from a channel without being in it (it is
    their server). Actions that would expose or extend access to a channel's
    CONTENT (adding members, managing webhooks) additionally require the
    caller to be a member: those handlers call require_member() first.
  * Space access: a member of the space, or a server admin.
  * Space manager: a member with the manager role, or a server admin.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, ChannelMember, Space, SpaceMember, User
from .vocab import ChannelRole, SpaceRole

# ---- channels ----


async def require_member(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    channel = await db.get(Channel, channel_id)
    member = await db.get(ChannelMember, (channel_id, user.id))
    if channel is None or member is None:
        # 404 for non-members too: don't reveal that a private channel exists.
        raise HTTPException(404, "Channel not found")
    return channel


async def is_channel_manager(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> bool:
    """Channel owner, or a server admin (member or not)."""
    if user.is_admin:
        return True
    member = await db.get(ChannelMember, (channel_id, user.id))
    return member is not None and member.role == ChannelRole.OWNER


async def require_channel_manager(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    """Channel owner or a server admin; admins can manage channels they
    aren't members of (it's their server)."""
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if user.is_admin:
        return channel
    if await db.get(ChannelMember, (channel_id, user.id)) is None:
        raise HTTPException(404, "Channel not found")
    if not await is_channel_manager(db, channel_id, user):
        raise HTTPException(403, "Only the channel owner can do that")
    return channel


# ---- spaces ----


async def can_access_space(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> bool:
    """A member of the space, or a server admin."""
    if user.is_admin:
        return True
    return await db.get(SpaceMember, (space_id, user.id)) is not None


async def is_space_manager(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> bool:
    """A member holding the manager role, or a server admin."""
    if user.is_admin:
        return True
    member = await db.get(SpaceMember, (space_id, user.id))
    return member is not None and member.role == SpaceRole.MANAGER


async def require_space_member(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> Space:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not await can_access_space(db, space_id, user):
        raise HTTPException(404, "Space not found")  # don't leak existence
    return space


async def require_space_manager(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> Space:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not await can_access_space(db, space_id, user):
        raise HTTPException(404, "Space not found")
    if not await is_space_manager(db, space_id, user):
        raise HTTPException(403, "Only a space manager can do that")
    return space
