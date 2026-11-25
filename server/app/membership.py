"""Who belongs to what, and the invariants that keep it sane: new users land in
the default space, joining a space joins its public channels, and a channel or
space with members never ends up without an owner or manager."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events
from .models import Channel, ChannelMember, Space, SpaceMember
from .vocab import ChannelRole, SpaceRole


async def channel_member_ids(db: AsyncSession, channel_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.scalars(
                select(ChannelMember.user_id).where(
                    ChannelMember.channel_id == channel_id
                )
            )
        ).all()
    )


async def space_member_ids(db: AsyncSession, space_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.scalars(
                select(SpaceMember.user_id).where(SpaceMember.space_id == space_id)
            )
        ).all()
    )


async def ensure_default_space(db: AsyncSession, creator_id: uuid.UUID) -> Space:
    """The 'everyone' space new users auto-join. Created on demand."""
    space = await db.scalar(select(Space).where(Space.is_default == True))  # noqa: E712
    if space is None:
        space = Space(name="Main", is_default=True, created_by=creator_id)
        db.add(space)
        await db.flush()
    return space


async def add_to_space(
    db: AsyncSession,
    space_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = SpaceRole.MEMBER,
) -> None:
    """Add a user to a space and auto-join its public channels."""
    if await db.get(SpaceMember, (space_id, user_id)) is None:
        db.add(SpaceMember(space_id=space_id, user_id=user_id, role=role))
    public = (
        await db.scalars(
            select(Channel.id).where(
                Channel.space_id == space_id,
                Channel.is_private == False,  # noqa: E712
                Channel.is_dm == False,  # noqa: E712
            )
        )
    ).all()
    if not public:
        return
    # One query for the memberships the user already has (was one get per channel).
    already = set(
        (
            await db.scalars(
                select(ChannelMember.channel_id).where(
                    ChannelMember.channel_id.in_(public),
                    ChannelMember.user_id == user_id,
                )
            )
        ).all()
    )
    for channel_id in public:
        if channel_id not in already:
            db.add(ChannelMember(channel_id=channel_id, user_id=user_id))


async def join_default_space(db: AsyncSession, user_id: uuid.UUID, is_admin: bool) -> None:
    """Every new account joins the default space (and its public channels); the
    first admin becomes its manager."""
    space = await ensure_default_space(db, user_id)
    await add_to_space(
        db, space.id, user_id, SpaceRole.MANAGER if is_admin else SpaceRole.MEMBER
    )


async def ensure_channel_owner(db: AsyncSession, channel_id: uuid.UUID) -> None:
    """If a channel is left with members but no owner, the longest-standing
    remaining member inherits the role, so routine management (members, rename,
    webhooks) never needs an admin. An empty channel simply stays ownerless."""
    has_owner = await db.scalar(
        select(ChannelMember.user_id).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.role == ChannelRole.OWNER,
        ).limit(1)
    )
    if has_owner is not None:
        return
    heir = await db.scalar(
        select(ChannelMember)
        .where(ChannelMember.channel_id == channel_id)
        .order_by(ChannelMember.joined_at, ChannelMember.user_id)
        .limit(1)
    )
    if heir is not None:
        heir.role = ChannelRole.OWNER
        await db.flush()
        events.publish(db, [heir.user_id], {"type": "channels.changed"})


async def ensure_space_manager(db: AsyncSession, space_id: uuid.UUID) -> None:
    """If a space is left with members but no manager, the longest-standing
    member inherits the role so the space never depends on a server admin for
    routine membership changes."""
    has_mgr = await db.scalar(
        select(SpaceMember.user_id).where(
            SpaceMember.space_id == space_id, SpaceMember.role == SpaceRole.MANAGER
        ).limit(1)
    )
    if has_mgr is not None:
        return
    heir = await db.scalar(
        select(SpaceMember)
        .where(SpaceMember.space_id == space_id)
        .order_by(SpaceMember.joined_at, SpaceMember.user_id)
        .limit(1)
    )
    if heir is not None:
        heir.role = SpaceRole.MANAGER
        await db.flush()
        events.publish(db, [heir.user_id], {"type": "channels.changed"})
