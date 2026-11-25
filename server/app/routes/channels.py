import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import (
    can_access_space,
    is_channel_manager,
    is_space_manager,
    require_channel_manager,
    require_member,
)
from .. import events, storage, tasks
from ..db import DB, after_commit, commit
from ..deps import get_current_user
from ..membership import channel_member_ids, ensure_channel_owner, space_member_ids
from ..models import (
    THREAD_ACTIVE_DAYS,
    Channel,
    ChannelMember,
    ChannelNotifyPref,
    ChannelRead,
    File,
    Message,
    Reminder,
    Space,
    User,
    utcnow,
)
from ..schemas import (
    ChannelIn,
    ChannelMoveIn,
    ChannelOrderIn,
    ChannelOut,
    ChannelUpdateIn,
    DMIn,
    MemberIn,
    NotifyPrefIn,
    UserOut,
)
from ..vocab import DEFAULT_NOTIFY_LEVEL, ChannelRole

router = APIRouter(prefix="/api/v1", tags=["channels"])


async def channel_out(db: AsyncSession, channel: Channel, me: User) -> ChannelOut:
    """One channel, as the caller sees it. Deliberately the single-row case of
    channels_out_bulk() so a count or field can never differ between an
    endpoint that returns one channel and the sidebar listing."""
    return (await channels_out_bulk(db, [channel], me))[0]


@router.put("/channels/{channel_id}/notify", response_model=ChannelOut)
async def set_notify_pref(
    channel_id: uuid.UUID,
    body: NotifyPrefIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    channel = await require_member(db, channel_id, user)
    pref = await db.get(ChannelNotifyPref, (channel_id, user.id))
    if body.level == DEFAULT_NOTIFY_LEVEL:
        # Storing the default would just be noise — drop any explicit row.
        if pref is not None:
            await db.delete(pref)
    elif pref is not None:
        pref.level = body.level
    else:
        db.add(
            ChannelNotifyPref(channel_id=channel_id, user_id=user.id, level=body.level)
        )
    await db.flush()
    return await channel_out(db, channel, user)


@router.post("/channels/{channel_id}/read", status_code=204)
async def mark_read(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    await require_member(db, channel_id, user)
    read = await db.get(ChannelRead, (channel_id, user.id))
    if read is not None:
        read.last_read_at = utcnow()
    else:
        db.add(ChannelRead(channel_id=channel_id, user_id=user.id, last_read_at=utcnow()))


async def channels_out_bulk(
    db: AsyncSession, channels: list[Channel], me: User
) -> list[ChannelOut]:
    """Build ChannelOut for many channels with a fixed number of grouped queries
    (not a handful per channel). The ONLY place a ChannelOut is assembled:
    channel_out() is this function called with one channel."""
    if not channels:
        return []
    ids = [c.id for c in channels]
    dm_ids = [c.id for c in channels if c.is_dm]
    non_dm_ids = [c.id for c in channels if not c.is_dm]
    now = utcnow()
    week_ago = now - timedelta(days=7)
    thread_cutoff = now - timedelta(days=THREAD_ACTIVE_DAYS)

    async def counts(*conds, distinct_col=None) -> dict:
        col = func.count(func.distinct(distinct_col)) if distinct_col is not None else func.count()
        rows = (
            await db.execute(
                select(Message.channel_id, col)
                .where(Message.channel_id.in_(ids), *conds)
                .group_by(Message.channel_id)
            )
        ).all()
        return {cid: n for cid, n in rows}

    total_by = await counts(Message.deleted_at.is_(None), Message.thread_root_id.is_(None))
    recent_by = await counts(
        Message.deleted_at.is_(None),
        Message.thread_root_id.is_(None),
        Message.created_at >= week_ago,
    )
    pinned_by = await counts(Message.deleted_at.is_(None), Message.pinned_at.is_not(None))
    thread_by = await counts(
        Message.deleted_at.is_(None),
        Message.thread_root_id.is_not(None),
        Message.created_at >= thread_cutoff,
        distinct_col=Message.thread_root_id,
    )

    # Unread: not mine, after my per-channel read marker (or all if never read).
    unread_by = {
        cid: n
        for cid, n in (
            await db.execute(
                select(Message.channel_id, func.count())
                .select_from(Message)
                .outerjoin(
                    ChannelRead,
                    and_(
                        ChannelRead.channel_id == Message.channel_id,
                        ChannelRead.user_id == me.id,
                    ),
                )
                .where(
                    Message.channel_id.in_(ids),
                    Message.deleted_at.is_(None),
                    Message.sender_id != me.id,
                    or_(
                        ChannelRead.last_read_at.is_(None),
                        Message.created_at > ChannelRead.last_read_at,
                    ),
                )
                .group_by(Message.channel_id)
            )
        ).all()
    }

    reminder_by = {
        cid: n
        for cid, n in (
            await db.execute(
                select(Reminder.channel_id, func.count())
                .where(
                    Reminder.channel_id.in_(ids),
                    Reminder.user_id == me.id,
                    Reminder.fired_at.is_(None),
                )
                .group_by(Reminder.channel_id)
            )
        ).all()
    }
    role_by = dict(
        (
            await db.execute(
                select(ChannelMember.channel_id, ChannelMember.role).where(
                    ChannelMember.channel_id.in_(ids), ChannelMember.user_id == me.id
                )
            )
        ).all()
    )
    pref_by = dict(
        (
            await db.execute(
                select(ChannelNotifyPref.channel_id, ChannelNotifyPref.level).where(
                    ChannelNotifyPref.channel_id.in_(ids),
                    ChannelNotifyPref.user_id == me.id,
                )
            )
        ).all()
    )

    # Open task counts: one scan of just the task-bearing messages, tallied per
    # channel (checkbox lines that are still unchecked).
    task_by: dict = {}
    if non_dm_ids:
        # Sum unchecked task lines per channel in SQL. This backs every sidebar
        # refresh; it used to ship the full body of every task-bearing message
        # across all the user's channels and re-parse them in Python each time.
        for cid, n in (
            await db.execute(
                select(Message.channel_id, func.sum(tasks.open_task_count(Message.content)))
                .where(
                    Message.channel_id.in_(non_dm_ids),
                    Message.deleted_at.is_(None),
                    tasks.may_have_open_task(Message.content),
                )
                .group_by(Message.channel_id)
            )
        ).all():
            task_by[cid] = int(n or 0)

    # DM participants, bulk-fetched.
    dm_members_by: dict = {}
    if dm_ids:
        member_rows = (
            await db.execute(
                select(ChannelMember.channel_id, ChannelMember.user_id).where(
                    ChannelMember.channel_id.in_(dm_ids),
                    ChannelMember.user_id != me.id,
                )
            )
        ).all()
        wanted = {uid for _, uid in member_rows}
        users_by = {}
        if wanted:
            users_by = {
                u.id: u
                for u in (await db.scalars(select(User).where(User.id.in_(wanted)))).all()
            }
        for cid, uid in member_rows:
            u = users_by.get(uid)
            if u:
                dm_members_by.setdefault(cid, []).append(u)

    out: list[ChannelOut] = []
    for c in channels:
        dm_user = None
        dm_members: list[UserOut] = []
        if c.is_dm:
            mems = dm_members_by.get(c.id, [])
            if len(mems) == 1:
                dm_user = UserOut.model_validate(mems[0])
            elif mems:
                dm_members = [UserOut.model_validate(m) for m in mems]
        out.append(
            ChannelOut(
                id=c.id,
                name=c.name,
                topic=c.topic,
                is_private=c.is_private,
                is_dm=c.is_dm,
                space_id=c.space_id,
                dm_user=dm_user,
                dm_members=dm_members,
                my_role=role_by.get(c.id),
                message_count=total_by.get(c.id, 0),
                recent_count=recent_by.get(c.id, 0),
                unread_count=unread_by.get(c.id, 0),
                open_task_count=task_by.get(c.id, 0),
                reminder_count=reminder_by.get(c.id, 0),
                pinned_count=pinned_by.get(c.id, 0),
                thread_count=thread_by.get(c.id, 0),
                notify_level=pref_by.get(c.id) or DEFAULT_NOTIFY_LEVEL,
            )
        )
    return out


@router.get("/channels", response_model=list[ChannelOut])
async def my_channels(
    db: AsyncSession = DB, user: User = Depends(get_current_user)
) -> list[ChannelOut]:
    channels = (
        await db.scalars(
            select(Channel)
            .join(ChannelMember, ChannelMember.channel_id == Channel.id)
            .where(ChannelMember.user_id == user.id)
            .order_by(Channel.position, Channel.created_at)
        )
    ).all()
    return await channels_out_bulk(db, list(channels), user)


@router.get("/channels/browse", response_model=list[ChannelOut])
async def browse_channels(
    space_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[ChannelOut]:
    # Only within a space the caller belongs to.
    if not await can_access_space(db, space_id, user):
        raise HTTPException(404, "Space not found")
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    channels = (
        await db.scalars(
            select(Channel)
            .where(
                Channel.space_id == space_id,
                Channel.is_private == False,  # noqa: E712
                Channel.is_dm == False,  # noqa: E712
                Channel.id.not_in(mine),
            )
            .order_by(Channel.position, Channel.created_at)
        )
    ).all()
    return await channels_out_bulk(db, list(channels), user)


@router.put("/channels/order", status_code=204)
async def reorder_channels(
    body: ChannelOrderIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    """Set the top-to-bottom order of a space's channels (space manager or admin).
    `order` is channel ids in the desired order; each gets position = its index."""
    space = await db.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not await is_space_manager(db, space.id, user):
        raise HTTPException(403, "Only a space manager can reorder its channels")
    # One statement instead of one UPDATE per channel: CASE maps each id to its
    # index. Scoped to this space so foreign ids in `order` are inert.
    positions = {cid: i for i, cid in enumerate(body.order)}
    if positions:
        await db.execute(
            update(Channel)
            .where(Channel.id.in_(list(positions)), Channel.space_id == space.id)
            .values(position=case(positions, value=Channel.id))
        )
    await db.flush()
    # The order is global, so nudge everyone in the space to re-sort their sidebar.
    events.publish(
        db, await space_member_ids(db, space.id), {"type": "channels.changed"}
    )


@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    body: ChannelIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    # Must be a member of the space the channel lives in.
    if not await can_access_space(db, body.space_id, user):
        raise HTTPException(403, "You are not a member of that space")
    name = body.name.strip()
    existing = await db.scalar(
        select(Channel).where(
            Channel.name == name,
            Channel.space_id == body.space_id,
            Channel.is_dm == False,  # noqa: E712
        )
    )
    if existing:
        raise HTTPException(409, "A channel with that name already exists in this space")
    max_pos = await db.scalar(
        select(func.max(Channel.position)).where(Channel.space_id == body.space_id)
    )
    channel = Channel(
        name=name, is_private=body.is_private, space_id=body.space_id,
        created_by=user.id, position=(max_pos or 0) + 1,
    )
    db.add(channel)
    try:
        await db.flush()
    except IntegrityError:
        # Two requests raced past the check above; the unique index decides.
        await db.rollback()
        raise HTTPException(409, "A channel with that name already exists in this space")
    db.add(
        ChannelMember(channel_id=channel.id, user_id=user.id, role=ChannelRole.OWNER)
    )
    # Public channels are visible to the whole space: enroll its members.
    if not body.is_private:
        member_ids = await space_member_ids(db, body.space_id)
        for member_id in member_ids:
            if member_id != user.id:
                db.add(ChannelMember(channel_id=channel.id, user_id=member_id))
        await db.flush()
        events.publish(
            db, [m for m in member_ids if m != user.id], {"type": "channels.changed"}
        )
    return await channel_out(db, channel, user)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdateIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    channel = await require_channel_manager(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "DMs can't be renamed")
    if body.name is not None:
        name = body.name.strip()
        if name != channel.name:
            # Same-space uniqueness, matching create_channel — without the
            # space filter a rename 409'd if the name existed in ANY space.
            existing = await db.scalar(
                select(Channel).where(
                    Channel.name == name,
                    Channel.space_id == channel.space_id,
                    Channel.is_dm == False,  # noqa: E712
                )
            )
            if existing:
                raise HTTPException(409, "A channel with that name already exists")
            channel.name = name
    if body.topic is not None:
        channel.topic = body.topic.strip() or None
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A channel with that name already exists")
    events.publish(
        db,
        await channel_member_ids(db, channel_id),
        {"type": "channel.updated", "channel_id": str(channel_id)},
    )
    return await channel_out(db, channel, user)


@router.put("/channels/{channel_id}/space", response_model=ChannelOut)
async def move_channel(
    channel_id: uuid.UUID,
    body: ChannelMoveIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    """Move a channel to another space (owner or admin). Membership is
    reconciled to the destination space: a public channel picks up all of that
    space's members, and anyone no longer in the space is dropped, so nobody is
    left subscribed to a channel they can no longer see."""
    channel = await require_channel_manager(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "DMs don't belong to a space")
    space = await db.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not await can_access_space(db, space.id, user):
        raise HTTPException(403, "You must be a member of the destination space")
    if channel.space_id == space.id:
        return await channel_out(db, channel, user)
    # The destination may already have a channel by this name (create/rename
    # checked this; move never did, and the DB now enforces it).
    clash = await db.scalar(
        select(Channel.id).where(
            Channel.space_id == space.id,
            Channel.name == channel.name,
            Channel.is_dm == False,  # noqa: E712
        )
    )
    if clash is not None:
        raise HTTPException(409, "A channel with that name already exists in that space")

    old_members = set(await channel_member_ids(db, channel_id))
    channel.space_id = space.id
    max_pos = await db.scalar(
        select(func.max(Channel.position)).where(Channel.space_id == space.id)
    )
    channel.position = (max_pos or 0) + 1  # land at the bottom of the new space
    dest_members = set(await space_member_ids(db, space.id))
    current = (
        await db.scalars(
            select(ChannelMember).where(ChannelMember.channel_id == channel.id)
        )
    ).all()
    for cm in current:
        if cm.user_id not in dest_members:
            await db.delete(cm)
    if not channel.is_private:
        present = {cm.user_id for cm in current if cm.user_id in dest_members}
        for uid in dest_members - present:
            db.add(ChannelMember(channel_id=channel.id, user_id=uid))
    await db.flush()
    # Guarantee an owner remains (the mover, if the original owner was dropped).
    remaining = (
        await db.scalars(
            select(ChannelMember).where(ChannelMember.channel_id == channel.id)
        )
    ).all()
    if not any(m.role == ChannelRole.OWNER for m in remaining):
        mine = await db.get(ChannelMember, (channel.id, user.id))
        if mine is None:
            db.add(
                ChannelMember(
                    channel_id=channel.id, user_id=user.id, role=ChannelRole.OWNER
                )
            )
        else:
            mine.role = ChannelRole.OWNER
        await db.flush()
    affected = old_members | dest_members | {user.id}
    events.publish(db, list(affected), {"type": "channels.changed"})
    return await channel_out(db, channel, user)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    # Deliberately admin-only (not channel owners): deletion destroys all
    # messages and files, so it lives behind the admin settings panel.
    if not user.is_admin:
        raise HTTPException(403, "Only a server admin can delete channels")
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if channel.is_dm:
        raise HTTPException(400, "DMs can't be deleted")
    members = await channel_member_ids(db, channel_id)
    file_ids = (
        await db.scalars(select(File.id).where(File.channel_id == channel_id))
    ).all()
    await db.delete(channel)  # cascades members, messages, file rows
    events.publish(
        db, members, {"type": "channel.deleted", "channel_id": str(channel_id)}
    )
    after_commit(db, storage.unlink_blobs, file_ids)
    await commit(db)


@router.post("/channels/{channel_id}/join", response_model=ChannelOut)
async def join_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    channel = await db.get(Channel, channel_id)
    if channel is None or channel.is_private or channel.is_dm:
        raise HTTPException(404, "Channel not found")
    if channel.space_id is not None and not await can_access_space(
        db, channel.space_id, user
    ):
        raise HTTPException(404, "Channel not found")
    if await db.get(ChannelMember, (channel_id, user.id)) is None:
        db.add(ChannelMember(channel_id=channel_id, user_id=user.id))
    return await channel_out(db, channel, user)


@router.get("/channels/{channel_id}/members", response_model=list[UserOut])
async def list_members(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[User]:
    await require_member(db, channel_id, user)
    members = (
        await db.scalars(
            select(User)
            .join(ChannelMember, ChannelMember.user_id == User.id)
            .where(ChannelMember.channel_id == channel_id)
            .order_by(User.username)
        )
    ).all()
    return list(members)


@router.post("/channels/{channel_id}/members", response_model=UserOut, status_code=201)
async def add_member(
    channel_id: uuid.UUID,
    body: MemberIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> User:
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot add members to a DM")
    if not await is_channel_manager(db, channel_id, user):
        raise HTTPException(403, "Only the channel owner can add members")
    target = await db.get(User, body.user_id)
    if target is None or target.disabled:
        raise HTTPException(404, "User not found")
    # Keep the space boundary intact: a channel's members must belong to its
    # space (join_channel already enforces this; add_member didn't). Admins are
    # exempt, as elsewhere. Same 404 as join, so the check leaks nothing.
    if channel.space_id is not None and not await can_access_space(
        db, channel.space_id, target
    ):
        raise HTTPException(404, "User not found")
    if await db.get(ChannelMember, (channel_id, target.id)) is None:
        db.add(ChannelMember(channel_id=channel_id, user_id=target.id))
        await db.flush()
        events.publish(db, [target.id], {"type": "channels.changed"})
    return target


@router.delete("/channels/{channel_id}/members/me", status_code=204)
async def leave_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot leave a DM")
    member = await db.get(ChannelMember, (channel_id, user.id))
    was_owner = member.role == ChannelRole.OWNER
    await db.delete(member)
    await db.flush()
    if was_owner:
        await ensure_channel_owner(db, channel_id)


@router.delete("/channels/{channel_id}/members/{user_id}", status_code=204)
async def remove_member(
    channel_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    channel = await require_channel_manager(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot remove members from a DM")
    member = await db.get(ChannelMember, (channel_id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == ChannelRole.OWNER and not user.is_admin:
        raise HTTPException(403, "Only a server admin can remove the channel owner")
    await db.delete(member)
    await db.flush()
    events.publish(
        db, [user_id], {"type": "channel.deleted", "channel_id": str(channel_id)}
    )


@router.post("/dms", response_model=ChannelOut)
async def open_dm(
    body: DMIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> ChannelOut:
    """One target → a 1:1 DM; several → a group DM. Reuses an existing DM whose
    member set is exactly the same people."""
    target_ids = {uid for uid in body.user_ids if uid != user.id}
    if not target_ids:
        raise HTTPException(400, "Pick at least one other person")
    targets = (
        await db.scalars(
            select(User).where(User.id.in_(target_ids), User.disabled == False)  # noqa: E712
        )
    ).all()
    if len(targets) != len(target_ids):
        raise HTTPException(404, "User not found")
    target_set = {user.id} | target_ids

    # Reuse an existing DM whose members are exactly this set: fetch every
    # member row of every DM the caller is in with one grouped query, then
    # compare sets in Python (was one query per DM).
    my_dm_ids = select(ChannelMember.channel_id).join(
        Channel, Channel.id == ChannelMember.channel_id
    ).where(Channel.is_dm == True, ChannelMember.user_id == user.id)  # noqa: E712
    rows = (
        await db.execute(
            select(ChannelMember.channel_id, ChannelMember.user_id).where(
                ChannelMember.channel_id.in_(my_dm_ids)
            )
        )
    ).all()
    members_by_dm: dict[uuid.UUID, set[uuid.UUID]] = {}
    for cid, mid in rows:
        members_by_dm.setdefault(cid, set()).add(mid)
    for cid, member_set in members_by_dm.items():
        if member_set == target_set:
            channel = await db.get(Channel, cid)
            return await channel_out(db, channel, user)

    channel = Channel(is_dm=True, created_by=user.id)
    db.add(channel)
    await db.flush()
    for mid in target_set:
        db.add(ChannelMember(channel_id=channel.id, user_id=mid))
    await db.flush()
    events.publish(db, [t.id for t in targets], {"type": "channels.changed"})
    return await channel_out(db, channel, user)
