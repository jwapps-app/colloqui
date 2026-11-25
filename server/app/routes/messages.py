import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from .. import messaging, tasks
from ..access import is_channel_manager, require_member
from ..db import DB
from ..deps import get_current_user
from ..messaging import Part, serialize_messages
from ..models import (
    THREAD_ACTIVE_DAYS,
    Channel,
    ChannelMember,
    File,
    Message,
    MessageChange,
    Reaction,
    TaskCompletion,
    User,
    utcnow,
)
from ..schemas import (
    CheckboxIn,
    MessageIn,
    MessageOut,
    OpenTaskOut,
    ReactionAgg,
    ReactionIn,
    SearchHit,
    SyncOut,
    ThreadDigestOut,
)
from ..vocab import ALLOWED_EMOJI

router = APIRouter(prefix="/api/v1", tags=["messages"])


def _my_channel_ids(user: User):
    """Subquery: the channels the caller belongs to. Every cross-channel feed
    is scoped by this."""
    return select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)


def _with_sender_and_file(*extra):
    """Query: messages joined to their sender and (optional) file, the row
    shape serialize_messages() takes."""
    return (
        select(Message, User, File, *extra)
        .join(User, User.id == Message.sender_id)
        .outerjoin(File, File.id == Message.file_id)
    )


async def _live_message(db: AsyncSession, message_id: uuid.UUID, user: User) -> Message:
    """A message the caller can see: it exists, is not deleted, and they are in
    its channel."""
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    return message


# ---- cross-channel feeds ----


@router.get("/search/messages", response_model=list[SearchHit])
async def search_messages(
    q: str = Query(min_length=2, max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[SearchHit]:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await db.execute(
            _with_sender_and_file(Channel)
            .join(Channel, Channel.id == Message.channel_id)
            .where(
                Message.channel_id.in_(_my_channel_ids(user)),
                Message.deleted_at.is_(None),
                Message.content.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()
    return await _hits(db, rows)


async def _hits(db: AsyncSession, rows) -> list[SearchHit]:
    outs = await serialize_messages(
        db, [(m, u, f) for m, u, f, _ in rows], include=Part.BARE
    )
    return [
        SearchHit(channel_id=c.id, channel_name=c.name, is_dm=c.is_dm, message=out)
        for (_, _, _, c), out in zip(rows, outs)
    ]


@router.get("/tasks", response_model=list[OpenTaskOut])
async def open_tasks(
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[OpenTaskOut]:
    """Every open task line across the caller's channels."""
    rows = (
        await db.execute(
            select(Message, Channel)
            .join(Channel, Channel.id == Message.channel_id)
            .where(
                Message.channel_id.in_(_my_channel_ids(user)),
                Message.deleted_at.is_(None),
                tasks.may_have_open_task(Message.content),
            )
            .order_by(Message.created_at)
            .limit(1000)
        )
    ).all()
    return [
        OpenTaskOut(
            message_id=m.id,
            channel_id=c.id,
            channel_name=c.name,
            is_dm=c.is_dm,
            line=line,
            text=text,
            created_at=m.created_at,
        )
        for m, c in rows
        for line, text in tasks.open_tasks(m.content)
    ]


@router.get("/pins", response_model=list[SearchHit])
async def all_pins(
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[SearchHit]:
    """Every pinned message across the caller's channels, newest pin first."""
    rows = (
        await db.execute(
            _with_sender_and_file(Channel)
            .join(Channel, Channel.id == Message.channel_id)
            .where(
                Message.channel_id.in_(_my_channel_ids(user)),
                Message.deleted_at.is_(None),
                Message.pinned_at.is_not(None),
            )
            .order_by(Message.pinned_at.desc())
            .limit(500)  # bound the response like the other cross-channel feeds
        )
    ).all()
    return await _hits(db, rows)


@router.get("/threads", response_model=list[ThreadDigestOut])
async def my_threads(
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[ThreadDigestOut]:
    """Active threads the caller takes part in (started or replied to)."""
    cutoff = utcnow() - timedelta(days=THREAD_ACTIVE_DAYS)
    # Roots the user has replied in.
    replied = select(Message.thread_root_id).where(
        Message.sender_id == user.id,
        Message.thread_root_id.is_not(None),
        Message.deleted_at.is_(None),
    )
    # Roots the user authored that have at least one live reply.
    has_reply = select(Message.thread_root_id).where(
        Message.thread_root_id.is_not(None), Message.deleted_at.is_(None)
    )
    started = select(Message.id).where(
        Message.sender_id == user.id,
        Message.deleted_at.is_(None),
        Message.id.in_(has_reply),
    )
    root_ids = set((await db.scalars(replied)).all()) | set(
        (await db.scalars(started)).all()
    )
    if not root_ids:
        return []
    rows = (
        await db.execute(
            _with_sender_and_file(Channel)
            .join(Channel, Channel.id == Message.channel_id)
            .where(
                Message.id.in_(root_ids),
                Message.deleted_at.is_(None),
                Message.channel_id.in_(_my_channel_ids(user)),
            )
        )
    ).all()
    roots = await serialize_messages(
        db, [(m, u, f) for m, u, f, _ in rows], include=Part.DIGEST
    )
    digests = [
        ThreadDigestOut(channel_id=c.id, channel_name=c.name, is_dm=c.is_dm, root=root)
        for (_, _, _, c), root in zip(rows, roots)
    ]
    # Drop expired threads (no reply within the active window).
    digests = [d for d in digests if (d.root.thread_last_at or d.root.created_at) >= cutoff]
    digests.sort(
        key=lambda d: d.root.thread_last_at or d.root.created_at, reverse=True
    )
    return digests


@router.get("/sync", response_model=SyncOut)
async def sync(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> SyncOut:
    """Offline-sync delta: every message in the caller's channels that changed
    after the `since` cursor, as full message objects (deleted ones come
    through as tombstones carrying `deleted_at`). Bootstrap with the normal
    history endpoints, then poll this; call again with the returned `cursor`
    while `has_more`."""
    rows = (
        await db.execute(
            select(MessageChange.seq, Message, User, File)
            .join(Message, Message.id == MessageChange.message_id)
            .join(User, User.id == Message.sender_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(
                MessageChange.seq > since,
                MessageChange.channel_id.in_(_my_channel_ids(user)),
            )
            .order_by(MessageChange.seq)
            .limit(limit)
        )
    ).all()
    out = await serialize_messages(db, [(m, u, f) for _, m, u, f in rows])
    cursor = rows[-1][0] if rows else since
    return SyncOut(messages=out, cursor=cursor, has_more=len(rows) == limit)


# ---- one channel ----


@router.get("/channels/{channel_id}/messages", response_model=list[MessageOut])
async def list_messages(
    channel_id: uuid.UUID,
    before: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    await require_member(db, channel_id, user)
    query = _with_sender_and_file().where(
        Message.channel_id == channel_id,
        Message.deleted_at.is_(None),
        Message.thread_root_id.is_(None),  # thread replies live in the thread view
    )
    if before is not None:
        ref = await db.get(Message, before)
        if ref is not None and ref.channel_id == channel_id:
            query = query.where(
                tuple_(Message.created_at, Message.id) < (ref.created_at, ref.id)
            )
    rows = (
        await db.execute(
            query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
    ).all()
    return (await serialize_messages(db, [tuple(r) for r in rows]))[::-1]  # oldest first


@router.get("/channels/{channel_id}/threads", response_model=list[MessageOut])
async def channel_threads(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    """Active threads (a root with a reply in the last week) in a channel."""
    await require_member(db, channel_id, user)
    cutoff = utcnow() - timedelta(days=THREAD_ACTIVE_DAYS)
    root_ids = (
        await db.scalars(
            select(Message.thread_root_id)
            .where(
                Message.channel_id == channel_id,
                Message.thread_root_id.is_not(None),
                Message.deleted_at.is_(None),
                Message.created_at >= cutoff,
            )
            .distinct()
        )
    ).all()
    if not root_ids:
        return []
    rows = (
        await db.execute(
            _with_sender_and_file().where(
                Message.id.in_(root_ids), Message.deleted_at.is_(None)
            )
        )
    ).all()
    out = await serialize_messages(db, [tuple(r) for r in rows], include=Part.DIGEST)
    out.sort(key=lambda mo: mo.thread_last_at or mo.created_at, reverse=True)
    return out


@router.get("/channels/{channel_id}/pins", response_model=list[MessageOut])
async def list_pins(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    await require_member(db, channel_id, user)
    rows = (
        await db.execute(
            _with_sender_and_file()
            .where(
                Message.channel_id == channel_id,
                Message.deleted_at.is_(None),
                Message.pinned_at.is_not(None),
            )
            .order_by(Message.pinned_at.desc())
            .limit(500)  # bounded like the cross-channel pins feed
        )
    ).all()
    return await serialize_messages(
        db, [tuple(r) for r in rows], include=Part.REPLY_ONLY
    )


@router.get("/messages/{root_id}/thread", response_model=list[MessageOut])
async def list_thread(
    root_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    """The root message followed by every reply in its thread, oldest first.
    A deleted root is returned as a tombstone so its live replies stay
    reachable (the thread accepts no NEW posts, see messaging.post_message)."""
    root = await db.get(Message, root_id)
    if root is None:
        raise HTTPException(404, "Thread not found")
    # If a reply id was passed, resolve to its actual root.
    if root.thread_root_id is not None:
        root = await db.get(Message, root.thread_root_id)
        if root is None:
            raise HTTPException(404, "Thread not found")
    await require_member(db, root.channel_id, user)
    rows = (
        await db.execute(
            _with_sender_and_file()
            .where(
                (Message.id == root.id)
                | ((Message.thread_root_id == root.id) & Message.deleted_at.is_(None)),
            )
            .order_by(Message.created_at, Message.id)
            .limit(2000)  # bound the response; threads never realistically hit this
        )
    ).all()
    return await serialize_messages(
        db,
        [tuple(r) for r in rows],
        include=Part.IN_THREAD,
        tombstone_text="(message deleted)",  # only ever the root
    )


# ---- posting and changing ----


@router.post(
    "/channels/{channel_id}/messages", response_model=MessageOut, status_code=201
)
async def send_message(
    channel_id: uuid.UUID,
    body: MessageIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> MessageOut:
    channel = await require_member(db, channel_id, user)
    return await messaging.post_message(
        db,
        channel,
        user,
        body.content,
        file_id=body.file_id,
        reply_to_id=body.reply_to_id,
        thread_root_id=body.thread_root_id,
        client_id=body.id,
    )


@router.post("/messages/{message_id}/checkbox", response_model=MessageOut)
async def toggle_checkbox(
    message_id: uuid.UUID,
    body: CheckboxIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> MessageOut:
    """Any channel member may flip a `[ ]`/`[x]` task line — a deliberate,
    narrow exception to sender-only editing: shared task lists are the point.
    Only the checkbox state can change, never the text."""
    # Row lock: two people ticking different lines read-modify-write the same
    # content string, and the second write silently reverted the first.
    message = await db.scalar(
        select(Message).where(Message.id == message_id).with_for_update()
    )
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    try:
        message.content = tasks.set_checked(message.content, body.line, body.checked)
    except tasks.NoSuchLine:
        raise HTTPException(400, "No such line")
    except tasks.NotATaskLine:
        raise HTTPException(400, "Not a task line")
    # Deliberately NOT marking edited_at — checking a box isn't an edit.
    existing = await db.get(TaskCompletion, (message_id, body.line))
    if body.checked and existing is None:
        db.add(TaskCompletion(message_id=message_id, line=body.line, cleared_by=user.id))
    elif not body.checked and existing is not None:
        await db.delete(existing)
    sender = await db.get(User, message.sender_id)
    return await messaging.publish_update(db, message, sender, tell_integrations=True)


async def _set_pin(
    db: AsyncSession, message_id: uuid.UUID, user: User, pinned: bool
) -> MessageOut:
    message = await _live_message(db, message_id, user)
    message.pinned_at = utcnow() if pinned else None
    message.pinned_by = user.id if pinned else None
    sender = await db.get(User, message.sender_id)
    return await messaging.publish_update(
        db,
        message,
        sender,
        tell_integrations=False,
        also={"type": "pins.changed", "channel_id": str(message.channel_id)},
    )


@router.post("/messages/{message_id}/pin", response_model=MessageOut)
async def pin_message(
    message_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> MessageOut:
    return await _set_pin(db, message_id, user, True)


@router.delete("/messages/{message_id}/pin", response_model=MessageOut)
async def unpin_message(
    message_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> MessageOut:
    return await _set_pin(db, message_id, user, False)


@router.post("/messages/{message_id}/reactions", response_model=list[ReactionAgg])
async def toggle_reaction(
    message_id: uuid.UUID,
    body: ReactionIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[ReactionAgg]:
    if body.emoji not in ALLOWED_EMOJI:
        raise HTTPException(400, "Unsupported reaction")
    message = await _live_message(db, message_id, user)
    existing = await db.get(Reaction, (message_id, user.id, body.emoji))
    if existing:
        await db.delete(existing)
    else:
        db.add(Reaction(message_id=message_id, user_id=user.id, emoji=body.emoji))
    return await messaging.publish_reactions(db, message)


@router.patch("/messages/{message_id}", response_model=MessageOut)
async def edit_message(
    message_id: uuid.UUID,
    body: MessageIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> MessageOut:
    message = await _live_message(db, message_id, user)
    if message.sender_id != user.id:
        raise HTTPException(403, "You can only edit your own messages")
    content = body.content.strip()
    if not content and message.file_id is None:
        raise HTTPException(400, "Message cannot be empty")
    message.content = content
    message.edited_at = utcnow()
    # Line numbers may have shifted — drop completion timestamps for this message.
    await db.execute(
        delete(TaskCompletion).where(TaskCompletion.message_id == message.id)
    )
    return await messaging.publish_update(
        db, message, user, tell_integrations=True, refetch_previews=True
    )


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    message = await _live_message(db, message_id, user)
    if message.sender_id != user.id:
        # Channel owners/admins may remove webhook-posted messages.
        can_manage = await is_channel_manager(db, message.channel_id, user)
        if not (message.webhook_name and can_manage):
            raise HTTPException(403, "You can only delete your own messages")
    await messaging.publish_deletion(db, message)
