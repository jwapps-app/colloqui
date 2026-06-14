import re
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
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
    SpaceMember,
    User,
    utcnow,
)
from ..schemas import (
    ChannelIn,
    ChannelOut,
    ChannelUpdateIn,
    DMIn,
    MemberIn,
    NotifyPrefIn,
    UserOut,
)


def default_notify_level(channel: Channel) -> str:
    return "all"
from ..ws import manager

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
        others = [u for u in [await db.get(User, oid) for oid in other_ids] if u]
        if len(others) == 1:
            dm_user = UserOut.model_validate(others[0])  # classic 1:1 DM
        elif others:
            dm_members = [UserOut.model_validate(o) for o in others]  # group DM
    my_member = await db.get(ChannelMember, (channel.id, me.id))
    total = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.channel_id == channel.id, Message.deleted_at.is_(None))
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
        task_msgs = await db.scalars(
            select(Message.content).where(
                Message.channel_id == channel.id,
                Message.deleted_at.is_(None),
                Message.content.ilike("%[ ] %"),
            )
        )
        for content in task_msgs:
            open_tasks += sum(
                1 for line in content.split("\n") if re.match(r"^\[ \] .", line)
            )
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
    notify_level = pref.level if pref else default_notify_level(channel)
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
    if body.level == default_notify_level(channel):
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


@router.get("/channels", response_model=list[ChannelOut])
async def my_channels(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ChannelOut]:
    channels = (
        await db.scalars(
            select(Channel)
            .join(ChannelMember, ChannelMember.channel_id == Channel.id)
            .where(ChannelMember.user_id == user.id)
            .order_by(Channel.created_at)
        )
    ).all()
    return [await channel_out(db, c, user) for c in channels]


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
            .order_by(Channel.created_at)
        )
    ).all()
    return [await channel_out(db, c, user) for c in channels]


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
    channel = Channel(
        name=name, is_private=body.is_private, space_id=body.space_id, created_by=user.id
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
            existing = await db.scalar(
                select(Channel).where(
                    Channel.name == name, Channel.is_dm == False  # noqa: E712
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
    targets = []
    for uid in set(body.user_ids):
        if uid == user.id:
            continue
        u = await db.get(User, uid)
        if u is None or u.disabled:
            raise HTTPException(404, "User not found")
        targets.append(u)
    if not targets:
        raise HTTPException(400, "Pick at least one other person")
    target_set = {user.id} | {t.id for t in targets}

    # Reuse an existing DM whose members are exactly this set.
    my_dm_ids = (
        await db.scalars(
            select(ChannelMember.channel_id)
            .join(Channel, Channel.id == ChannelMember.channel_id)
            .where(Channel.is_dm == True, ChannelMember.user_id == user.id)  # noqa: E712
        )
    ).all()
    for cid in my_dm_ids:
        if set(await member_ids(db, cid)) == target_set:
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
