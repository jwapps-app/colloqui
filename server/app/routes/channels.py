import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
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
    SpaceMember,
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


from ..ws import manager

# What members hear before they pick a per-channel preference.
DEFAULT_NOTIFY_LEVEL = "all"

# Unchecked task lines in a message body, counted in SQL. The "n" flag makes ^
# anchor at each line start and keeps . from crossing a newline, matching the
# per-line `^\[ \] .` the client renders as a checkbox. Requires Postgres 15+.
OPEN_TASK_COUNT = func.regexp_count(Message.content, r"^\[ \] .", 1, "n")

router = APIRouter(prefix="/api/v1", tags=["channels"])


async def require_member(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    channel = await db.get(Channel, channel_id)
    member = await db.get(ChannelMember, (channel_id, user.id))
    if channel is None or member is None:
        # 404 for non-members too: don't reveal that a private channel exists.
        raise HTTPException(404, "Channel not found")
    return channel


async def require_manageable(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    """Channel owner or a server admin; admins can manage channels they
    aren't members of (it's their server)."""
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if user.is_admin:
        return channel
    member = await db.get(ChannelMember, (channel_id, user.id))
    if member is None:
        raise HTTPException(404, "Channel not found")
    if member.role != "owner":
        raise HTTPException(403, "Only the channel owner can do that")
    return channel


async def member_ids(db: AsyncSession, channel_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.scalars(
                select(ChannelMember.user_id).where(
                    ChannelMember.channel_id == channel_id
                )
            )
        ).all()
    )


async def channel_out(db: AsyncSession, channel: Channel, me: User) -> ChannelOut:
    dm_user = None
    dm_members: list[UserOut] = []
    if channel.is_dm:
        other_ids = (
            await db.scalars(
                select(ChannelMember.user_id).where(
                    ChannelMember.channel_id == channel.id,
                    ChannelMember.user_id != me.id,
                )
            )
        ).all()
        others = (
            (await db.scalars(select(User).where(User.id.in_(other_ids)))).all()
            if other_ids else []
        )
        if len(others) == 1:
            dm_user = UserOut.model_validate(others[0])  # classic 1:1 DM
        elif others:
            dm_members = [UserOut.model_validate(o) for o in others]  # group DM
    my_member = await db.get(ChannelMember, (channel.id, me.id))
    # Counts mirror what the channel timeline shows: top-level (non-thread-reply),
    # non-deleted messages. (Thread replies live in the thread view; counting
    # them would show a number on a channel that looks empty.)
    total = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.channel_id == channel.id,
            Message.deleted_at.is_(None),
            Message.thread_root_id.is_(None),
        )
    )
    # Trailing 7-day message count — a feel for the channel's *current* activity.
    recent = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.channel_id == channel.id,
            Message.deleted_at.is_(None),
            Message.thread_root_id.is_(None),
            Message.created_at >= utcnow() - timedelta(days=7),
        )
    )
    read = await db.get(ChannelRead, (channel.id, me.id))
    unread_conds = [
        Message.channel_id == channel.id,
        Message.deleted_at.is_(None),
        Message.sender_id != me.id,
    ]
    if read is not None:
        unread_conds.append(Message.created_at > read.last_read_at)
    unread = await db.scalar(
        select(func.count()).select_from(Message).where(*unread_conds)
    )
    open_tasks = 0
    if not channel.is_dm:
        # Count unchecked task lines in SQL (regexp_count with the newline-
        # sensitive flag so ^ anchors each line) instead of shipping every
        # task-bearing message body to Python and re-parsing it per request.
        open_tasks = (
            await db.scalar(
                select(func.coalesce(func.sum(OPEN_TASK_COUNT), 0)).where(
                    Message.channel_id == channel.id,
                    Message.deleted_at.is_(None),
                    Message.content.ilike("%[ ] %"),
                )
            )
        ) or 0
    reminders = await db.scalar(
        select(func.count())
        .select_from(Reminder)
        .where(
            Reminder.user_id == me.id,
            Reminder.channel_id == channel.id,
            Reminder.fired_at.is_(None),
        )
    )
    pinned = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.channel_id == channel.id,
            Message.deleted_at.is_(None),
            Message.pinned_at.is_not(None),
        )
    )
    # Active threads: distinct roots that have had a reply within the window.
    cutoff = utcnow() - timedelta(days=THREAD_ACTIVE_DAYS)
    active_threads = await db.scalar(
        select(func.count(func.distinct(Message.thread_root_id))).where(
            Message.channel_id == channel.id,
            Message.thread_root_id.is_not(None),
            Message.deleted_at.is_(None),
            Message.created_at >= cutoff,
        )
    )
    pref = await db.get(ChannelNotifyPref, (channel.id, me.id))
    notify_level = pref.level if pref else DEFAULT_NOTIFY_LEVEL
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        topic=channel.topic,
        is_private=channel.is_private,
        is_dm=channel.is_dm,
        space_id=channel.space_id,
        dm_user=dm_user,
        dm_members=dm_members,
        my_role=my_member.role if my_member else None,
        message_count=total or 0,
        recent_count=recent or 0,
        unread_count=unread or 0,
        open_task_count=open_tasks,
        reminder_count=reminders or 0,
        pinned_count=pinned or 0,
        thread_count=active_threads or 0,
        notify_level=notify_level,
    )


@router.put("/channels/{channel_id}/notify", response_model=ChannelOut)
async def set_notify_pref(
    channel_id: uuid.UUID,
    body: NotifyPrefIn,
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
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
    instead of ~9 per channel. Same output as channel_out(), used by the hot
    my_channels listing."""
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
                select(Message.channel_id, func.sum(OPEN_TASK_COUNT))
                .where(
                    Message.channel_id.in_(non_dm_ids),
                    Message.deleted_at.is_(None),
                    Message.content.ilike("%[ ] %"),
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
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChannelOut]:
    # Only within a space the caller belongs to.
    if not user.is_admin and await db.get(SpaceMember, (space_id, user.id)) is None:
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Set the top-to-bottom order of a space's channels (space manager or admin).
    `order` is channel ids in the desired order; each gets position = its index."""
    space = await db.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not user.is_admin:
        member = await db.get(SpaceMember, (space.id, user.id))
        if member is None or member.role != "manager":
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
    member_ids = (
        await db.scalars(
            select(SpaceMember.user_id).where(SpaceMember.space_id == space.id)
        )
    ).all()
    await manager.send_to_users(member_ids, {"type": "channels.changed"})


@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    body: ChannelIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChannelOut:
    # Must be a member of the space the channel lives in.
    if not user.is_admin and await db.get(SpaceMember, (body.space_id, user.id)) is None:
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
    await db.flush()
    db.add(ChannelMember(channel_id=channel.id, user_id=user.id, role="owner"))
    # Public channels are visible to the whole space: enroll its members.
    if not body.is_private:
        member_ids = (
            await db.scalars(
                select(SpaceMember.user_id).where(SpaceMember.space_id == body.space_id)
            )
        ).all()
        for member_id in member_ids:
            if member_id != user.id:
                db.add(ChannelMember(channel_id=channel.id, user_id=member_id))
        await db.flush()
        await manager.send_to_users(
            [m for m in member_ids if m != user.id], {"type": "channels.changed"}
        )
    return await channel_out(db, channel, user)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChannelOut:
    channel = await require_manageable(db, channel_id, user)
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
    await db.flush()
    await manager.send_to_users(
        await member_ids(db, channel_id),
        {"type": "channel.updated", "channel_id": str(channel_id)},
    )
    return await channel_out(db, channel, user)


@router.put("/channels/{channel_id}/space", response_model=ChannelOut)
async def move_channel(
    channel_id: uuid.UUID,
    body: ChannelMoveIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChannelOut:
    """Move a channel to another space (owner or admin). Membership is
    reconciled to the destination space: a public channel picks up all of that
    space's members, and anyone no longer in the space is dropped, so nobody is
    left subscribed to a channel they can no longer see."""
    channel = await require_manageable(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "DMs don't belong to a space")
    space = await db.get(Space, body.space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if not user.is_admin and await db.get(SpaceMember, (space.id, user.id)) is None:
        raise HTTPException(403, "You must be a member of the destination space")
    if channel.space_id == space.id:
        return await channel_out(db, channel, user)

    old_members = set(await member_ids(db, channel_id))
    channel.space_id = space.id
    max_pos = await db.scalar(
        select(func.max(Channel.position)).where(Channel.space_id == space.id)
    )
    channel.position = (max_pos or 0) + 1  # land at the bottom of the new space
    dest_members = set(
        (
            await db.scalars(
                select(SpaceMember.user_id).where(SpaceMember.space_id == space.id)
            )
        ).all()
    )
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
    if not any(m.role == "owner" for m in remaining):
        mine = await db.get(ChannelMember, (channel.id, user.id))
        if mine is None:
            db.add(
                ChannelMember(channel_id=channel.id, user_id=user.id, role="owner")
            )
        else:
            mine.role = "owner"
        await db.flush()
    affected = old_members | dest_members | {user.id}
    await manager.send_to_users(list(affected), {"type": "channels.changed"})
    return await channel_out(db, channel, user)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
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
    members = await member_ids(db, channel_id)
    file_ids = (
        await db.scalars(select(File.id).where(File.channel_id == channel_id))
    ).all()
    await db.delete(channel)  # cascades members, messages, file rows
    await db.commit()
    for file_id in file_ids:
        (Path(settings.upload_dir) / str(file_id)).unlink(missing_ok=True)
    await manager.send_to_users(
        members, {"type": "channel.deleted", "channel_id": str(channel_id)}
    )


@router.post("/channels/{channel_id}/join", response_model=ChannelOut)
async def join_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChannelOut:
    channel = await db.get(Channel, channel_id)
    if channel is None or channel.is_private or channel.is_dm:
        raise HTTPException(404, "Channel not found")
    if (not user.is_admin and channel.space_id is not None
            and await db.get(SpaceMember, (channel.space_id, user.id)) is None):
        raise HTTPException(404, "Channel not found")
    if await db.get(ChannelMember, (channel_id, user.id)) is None:
        db.add(ChannelMember(channel_id=channel_id, user_id=user.id))
    return await channel_out(db, channel, user)


@router.get("/channels/{channel_id}/members", response_model=list[UserOut])
async def list_members(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot add members to a DM")
    me = await db.get(ChannelMember, (channel_id, user.id))
    if me.role != "owner" and not user.is_admin:
        raise HTTPException(403, "Only the channel owner can add members")
    target = await db.get(User, body.user_id)
    if target is None or target.disabled:
        raise HTTPException(404, "User not found")
    # Keep the space boundary intact: a channel's members must belong to its
    # space (join_channel already enforces this; add_member didn't). Admins are
    # exempt, as elsewhere. Same 404 as join, so the check leaks nothing.
    if (
        channel.space_id is not None
        and not target.is_admin
        and await db.get(SpaceMember, (channel.space_id, target.id)) is None
    ):
        raise HTTPException(404, "User not found")
    if await db.get(ChannelMember, (channel_id, target.id)) is None:
        db.add(ChannelMember(channel_id=channel_id, user_id=target.id))
        await db.flush()
        await manager.send_to_users([target.id], {"type": "channels.changed"})
    return target


@router.delete("/channels/{channel_id}/members/me", status_code=204)
async def leave_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot leave a DM")
    member = await db.get(ChannelMember, (channel_id, user.id))
    await db.delete(member)


@router.delete("/channels/{channel_id}/members/{user_id}", status_code=204)
async def remove_member(
    channel_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    channel = await require_manageable(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "Cannot remove members from a DM")
    member = await db.get(ChannelMember, (channel_id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == "owner" and not user.is_admin:
        raise HTTPException(403, "Only a server admin can remove the channel owner")
    await db.delete(member)
    await db.flush()
    await manager.send_to_users(
        [user_id], {"type": "channel.deleted", "channel_id": str(channel_id)}
    )


@router.post("/dms", response_model=ChannelOut)
async def open_dm(
    body: DMIn,
    db: AsyncSession = Depends(get_db),
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
    await manager.send_to_users(
        [t.id for t in targets], {"type": "channels.changed"}
    )
    return await channel_out(db, channel, user)
