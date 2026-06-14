import asyncio
import logging
import re
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import links
from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import (
    THREAD_ACTIVE_DAYS,
    Channel,
    ChannelMember,
    ChannelNotifyPref,
    File,
    LinkPreview,
    Message,
    MessageChange,
    Reaction,
    TaskCompletion,
    User,
    utcnow,
)
from ..notify import notify_user
from ..schemas import (
    CheckboxIn,
    FileOut,
    LinkPreviewOut,
    MessageIn,
    MessageOut,
    OpenTaskOut,
    ReactionAgg,
    ReactionIn,
    ReplyPreview,
    SearchHit,
    SyncOut,
    ThreadDigestOut,
    UserOut,
)
from ..security import hash_token

log = logging.getLogger("messages")
from ..ws import manager
from .channels import require_member

router = APIRouter(prefix="/api/v1", tags=["messages"])

# Stable sentinel identity for webhook-posted messages (never a real user, so
# no one sees edit/delete-as-self on them).
WEBHOOK_SENDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

ALLOWED_EMOJI = (
    "👍", "👎", "❤️", "🔥", "🎉", "😂", "😍", "🤔", "😮", "😢",
    "😡", "🙏", "👏", "✅", "❌", "⭐", "💯", "👀", "🚀", "✨",
    "🙌", "💪", "🤝", "👋", "🤣", "😅", "😊", "😎", "🥳", "😤",
    "😴", "🤯", "🤷", "🍕", "☕", "💔", "⚡", "📌", "➕", "🎯",
)


def message_out(
    message: Message,
    sender: User,
    file: File | None = None,
    reactions: list[ReactionAgg] | None = None,
    cleared: dict[str, object] | None = None,
    reply: ReplyPreview | None = None,
    thread: dict | None = None,
    previews: list[LinkPreviewOut] | None = None,
) -> MessageOut:
    thread = thread or {}
    if message.webhook_name:
        sender_out = UserOut(
            id=WEBHOOK_SENDER_ID,
            username="webhook",
            display_name=message.webhook_name,
            is_admin=False,
            avatar_at=None,
        )
    else:
        sender_out = UserOut.model_validate(sender)
    return MessageOut(
        id=message.id,
        channel_id=message.channel_id,
        sender=sender_out,
        content=message.content,
        created_at=message.created_at,
        edited_at=message.edited_at,
        file=FileOut.model_validate(file) if file else None,
        reactions=reactions or [],
        task_cleared=cleared or {},
        reply_to=reply,
        pinned=message.pinned_at is not None,
        link_previews=previews or [],
        deleted_at=message.deleted_at,
        thread_root_id=message.thread_root_id,
        reply_count=thread.get("count", 0),
        thread_last_at=thread.get("last_at"),
        thread_repliers=thread.get("repliers", []),
    )


async def record_change(db: AsyncSession, message: Message) -> None:
    """Stamp a message into the offline-sync change-log (upsert, one row per
    message) with a fresh monotonic seq. Call after any change to a message's
    rendered state (create/edit/delete/checkbox/pin/reaction)."""
    seq = await db.scalar(select(func.nextval("change_seq")))
    now = utcnow()
    stmt = (
        pg_insert(MessageChange)
        .values(
            message_id=message.id,
            channel_id=message.channel_id,
            seq=seq,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["message_id"],
            set_={"seq": seq, "channel_id": message.channel_id, "updated_at": now},
        )
    )
    await db.execute(stmt)


async def thread_meta_for(
    db: AsyncSession, root_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict]:
    """For each root message id, summarise its thread: reply count, time of the
    last reply, and a few distinct repliers (most recent first) for avatars."""
    if not root_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Message.thread_root_id,
                func.count().label("n"),
                func.max(Message.created_at).label("last_at"),
            )
            .where(
                Message.thread_root_id.in_(root_ids),
                Message.deleted_at.is_(None),
            )
            .group_by(Message.thread_root_id)
        )
    ).all()
    meta: dict[uuid.UUID, dict] = {
        root_id: {"count": n, "last_at": last_at, "repliers": []}
        for root_id, n, last_at in rows
    }
    if not meta:
        return {}
    # Distinct repliers per root, most-recent activity first, capped at 3.
    reply_rows = (
        await db.execute(
            select(Message.thread_root_id, User, func.max(Message.created_at).label("t"))
            .join(User, User.id == Message.sender_id)
            .where(
                Message.thread_root_id.in_(list(meta.keys())),
                Message.deleted_at.is_(None),
            )
            .group_by(Message.thread_root_id, User.id)
            .order_by(func.max(Message.created_at).desc())
        )
    ).all()
    for root_id, user, _ in reply_rows:
        bucket = meta[root_id]["repliers"]
        if len(bucket) < 3:
            bucket.append(UserOut.model_validate(user))
    return meta


async def thread_participant_ids(
    db: AsyncSession, root_id: uuid.UUID, exclude_id: uuid.UUID
) -> set[uuid.UUID]:
    """User ids who authored the root or any (live) reply in the thread."""
    rows = await db.scalars(
        select(Message.sender_id)
        .where(
            Message.deleted_at.is_(None),
            (Message.id == root_id) | (Message.thread_root_id == root_id),
        )
        .distinct()
    )
    return {r for r in rows if r != exclude_id}


async def reply_previews_for(
    db: AsyncSession, messages: list[Message]
) -> dict[uuid.UUID, ReplyPreview]:
    """Map each replying message id -> a compact preview of its parent."""
    parent_ids = {m.reply_to_id for m in messages if m.reply_to_id}
    if not parent_ids:
        return {}
    rows = (
        await db.execute(
            select(Message, User)
            .join(User, User.id == Message.sender_id)
            .where(Message.id.in_(parent_ids))
        )
    ).all()
    by_parent: dict[uuid.UUID, ReplyPreview] = {}
    for pm, ps in rows:
        if pm.deleted_at is not None:
            snippet = "(deleted message)"
        elif pm.content:
            snippet = pm.content[:80]
        else:
            snippet = "(attachment)"
        by_parent[pm.id] = ReplyPreview(
            id=pm.id, sender_name=ps.display_name, snippet=snippet
        )
    return {
        m.id: by_parent[m.reply_to_id]
        for m in messages
        if m.reply_to_id and m.reply_to_id in by_parent
    }


async def previews_for(
    db: AsyncSession, messages: list[Message]
) -> dict[uuid.UUID, list[LinkPreviewOut]]:
    """Cached (successful) link previews for each message's URLs."""
    per_msg: dict[uuid.UUID, list[str]] = {}
    wanted: set[str] = set()
    for m in messages:
        urls = links.extract_urls(m.content)
        if urls:
            per_msg[m.id] = urls
            wanted.update(hash_token(u) for u in urls)
    if not wanted:
        return {}
    rows = (
        await db.scalars(
            select(LinkPreview).where(
                LinkPreview.url_hash.in_(wanted),
                LinkPreview.ok == True,  # noqa: E712
            )
        )
    ).all()
    by_hash = {r.url_hash: r for r in rows}
    out: dict[uuid.UUID, list[LinkPreviewOut]] = {}
    for mid, urls in per_msg.items():
        cards = [
            LinkPreviewOut(
                url=by_hash[h].url,
                title=by_hash[h].title,
                description=by_hash[h].description,
                site_name=by_hash[h].site_name,
            )
            for h in (hash_token(u) for u in urls)
            if h in by_hash
        ]
        if cards:
            out[mid] = cards
    return out


async def _ensure_preview(db: AsyncSession, url: str) -> LinkPreview | None:
    h = hash_token(url)
    existing = await db.get(LinkPreview, h)
    if existing is not None:
        return existing
    meta = await links.fetch_metadata(url)
    row = LinkPreview(
        url_hash=h,
        url=url[:2000],
        ok=meta is not None,
        title=meta["title"] if meta else None,
        description=meta["description"] if meta else None,
        site_name=meta["site_name"] if meta else None,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:  # another message fetched the same URL concurrently
        await db.rollback()
        row = await db.get(LinkPreview, h)
    return row


async def _fetch_and_broadcast_previews(
    message_id: uuid.UUID, channel_id: uuid.UUID, urls: list[str]
) -> None:
    try:
        async with SessionLocal() as db:
            cards: list[LinkPreviewOut] = []
            for url in urls:
                pv = await _ensure_preview(db, url)
                if pv is not None and pv.ok:
                    cards.append(
                        LinkPreviewOut(
                            url=pv.url,
                            title=pv.title,
                            description=pv.description,
                            site_name=pv.site_name,
                        )
                    )
            await db.commit()
            if cards:
                await broadcast(
                    db,
                    channel_id,
                    {
                        "type": "message.preview",
                        "message_id": str(message_id),
                        "channel_id": str(channel_id),
                        "previews": jsonable_encoder(cards),
                    },
                )
    except Exception:
        log.exception("link preview fetch failed for message %s", message_id)


def schedule_previews(
    message_id: uuid.UUID, channel_id: uuid.UUID, content: str
) -> None:
    """Fire-and-forget: fetch link previews off the request path, then push
    them to clients over the WebSocket when ready."""
    urls = links.extract_urls(content)
    if urls:
        asyncio.create_task(
            _fetch_and_broadcast_previews(message_id, channel_id, urls)
        )


async def completions_for(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, object]]:
    if not message_ids:
        return {}
    rows = (
        await db.scalars(
            select(TaskCompletion).where(TaskCompletion.message_id.in_(message_ids))
        )
    ).all()
    out: dict[uuid.UUID, dict[str, object]] = {}
    for r in rows:
        out.setdefault(r.message_id, {})[str(r.line)] = r.cleared_at
    return out


async def reactions_for(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ReactionAgg]]:
    if not message_ids:
        return {}
    rows = (
        await db.scalars(select(Reaction).where(Reaction.message_id.in_(message_ids)))
    ).all()
    grouped: dict[uuid.UUID, dict[str, list[uuid.UUID]]] = {}
    for r in rows:
        grouped.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)
    return {
        mid: [
            ReactionAgg(emoji=emoji, count=len(users), user_ids=users)
            for emoji, users in sorted(
                emojis.items(),
                key=lambda kv: ALLOWED_EMOJI.index(kv[0]) if kv[0] in ALLOWED_EMOJI else 99,
            )
        ]
        for mid, emojis in grouped.items()
    }


async def broadcast(db: AsyncSession, channel_id: uuid.UUID, payload: dict) -> None:
    member_ids = (
        await db.scalars(
            select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
        )
    ).all()
    await manager.send_to_users(member_ids, payload)


@router.get("/search/messages", response_model=list[SearchHit])
async def search_messages(
    q: str = Query(min_length=2, max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SearchHit]:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    rows = (
        await db.execute(
            select(Message, User, File, Channel)
            .join(User, User.id == Message.sender_id)
            .join(Channel, Channel.id == Message.channel_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(
                Message.channel_id.in_(mine),
                Message.deleted_at.is_(None),
                Message.content.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        SearchHit(
            channel_id=c.id,
            channel_name=c.name,
            is_dm=c.is_dm,
            message=message_out(m, u, f),
        )
        for m, u, f, c in rows
    ]


@router.get("/tasks", response_model=list[OpenTaskOut])
async def open_tasks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[OpenTaskOut]:
    """Every unchecked `[ ]` task line across the caller's channels."""
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    rows = (
        await db.execute(
            select(Message, Channel)
            .join(Channel, Channel.id == Message.channel_id)
            .where(
                Message.channel_id.in_(mine),
                Message.deleted_at.is_(None),
                Message.content.ilike("%[ ] %"),
            )
            .order_by(Message.created_at)
            .limit(1000)
        )
    ).all()
    tasks: list[OpenTaskOut] = []
    for m, c in rows:
        for i, line in enumerate(m.content.split("\n")):
            if re.match(r"^\[ \] .", line):
                tasks.append(
                    OpenTaskOut(
                        message_id=m.id,
                        channel_id=c.id,
                        channel_name=c.name,
                        is_dm=c.is_dm,
                        line=i,
                        text=line[4:],
                        created_at=m.created_at,
                    )
                )
    return tasks


@router.get("/pins", response_model=list[SearchHit])
async def all_pins(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SearchHit]:
    """Every pinned message across the caller's channels, newest pin first."""
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    rows = (
        await db.execute(
            select(Message, User, File, Channel)
            .join(User, User.id == Message.sender_id)
            .join(Channel, Channel.id == Message.channel_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(
                Message.channel_id.in_(mine),
                Message.deleted_at.is_(None),
                Message.pinned_at.is_not(None),
            )
            .order_by(Message.pinned_at.desc())
        )
    ).all()
    return [
        SearchHit(
            channel_id=c.id,
            channel_name=c.name,
            is_dm=c.is_dm,
            message=message_out(m, u, f),
        )
        for m, u, f, c in rows
    ]


@router.get("/threads", response_model=list[ThreadDigestOut])
async def my_threads(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ThreadDigestOut]:
    """Active threads the caller takes part in (started or replied to)."""
    cutoff = utcnow() - timedelta(days=THREAD_ACTIVE_DAYS)
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
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
            select(Message, User, File, Channel)
            .join(User, User.id == Message.sender_id)
            .join(Channel, Channel.id == Message.channel_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(
                Message.id.in_(root_ids),
                Message.deleted_at.is_(None),
                Message.channel_id.in_(mine),
            )
        )
    ).all()
    ids = [m.id for m, _, _, _ in rows]
    aggs = await reactions_for(db, ids)
    comps = await completions_for(db, ids)
    threads = await thread_meta_for(db, ids)
    digests = [
        ThreadDigestOut(
            channel_id=c.id,
            channel_name=c.name,
            is_dm=c.is_dm,
            root=message_out(
                m, u, f, aggs.get(m.id), comps.get(m.id), None, threads.get(m.id)
            ),
        )
        for m, u, f, c in rows
    ]
    # Drop expired threads (no reply within the active window).
    digests = [d for d in digests if (d.root.thread_last_at or d.root.created_at) >= cutoff]
    digests.sort(
        key=lambda d: d.root.thread_last_at or d.root.created_at, reverse=True
    )
    return digests


@router.get("/channels/{channel_id}/messages", response_model=list[MessageOut])
async def list_messages(
    channel_id: uuid.UUID,
    before: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    await require_member(db, channel_id, user)
    query = (
        select(Message, User, File)
        .join(User, User.id == Message.sender_id)
        .outerjoin(File, File.id == Message.file_id)
        .where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
            Message.thread_root_id.is_(None),  # thread replies live in the thread view
        )
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
    msgs = [m for m, _, _ in rows]
    ids = [m.id for m in msgs]
    aggs = await reactions_for(db, ids)
    comps = await completions_for(db, ids)
    replies = await reply_previews_for(db, msgs)
    threads = await thread_meta_for(db, ids)
    previews = await previews_for(db, msgs)
    return [
        message_out(m, u, f, aggs.get(m.id), comps.get(m.id), replies.get(m.id),
                    threads.get(m.id), previews.get(m.id))
        for m, u, f in rows
    ][::-1]  # oldest first


@router.get("/sync", response_model=SyncOut)
async def sync(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SyncOut:
    """Offline-sync delta: every message in the caller's channels that changed
    after the `since` cursor, as full message objects (deleted ones carry
    `deleted_at` for tombstoning). Bootstrap with the normal history endpoints,
    then poll this; call again with the returned `cursor` while `has_more`."""
    mine = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    rows = (
        await db.execute(
            select(MessageChange.seq, Message, User, File)
            .join(Message, Message.id == MessageChange.message_id)
            .join(User, User.id == Message.sender_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(MessageChange.seq > since, MessageChange.channel_id.in_(mine))
            .order_by(MessageChange.seq)
            .limit(limit)
        )
    ).all()
    live = [(m, u, f) for _, m, u, f in rows if m.deleted_at is None]
    live_msgs = [m for m, _, _ in live]
    ids = [m.id for m in live_msgs]
    aggs = await reactions_for(db, ids)
    comps = await completions_for(db, ids)
    replies = await reply_previews_for(db, live_msgs)
    threads = await thread_meta_for(db, ids)
    previews = await previews_for(db, live_msgs)
    out: list[MessageOut] = []
    cursor = since
    for seq, m, u, f in rows:
        cursor = seq
        if m.deleted_at is not None:
            out.append(message_out(m, u))  # tombstone — deleted_at carries through
        else:
            out.append(
                message_out(m, u, f, aggs.get(m.id), comps.get(m.id),
                            replies.get(m.id), threads.get(m.id), previews.get(m.id))
            )
    return SyncOut(messages=out, cursor=cursor, has_more=len(rows) == limit)


@router.get("/channels/{channel_id}/threads", response_model=list[MessageOut])
async def channel_threads(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
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
            select(Message, User, File)
            .join(User, User.id == Message.sender_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(Message.id.in_(root_ids), Message.deleted_at.is_(None))
        )
    ).all()
    ids = [m.id for m, _, _ in rows]
    aggs = await reactions_for(db, ids)
    comps = await completions_for(db, ids)
    threads = await thread_meta_for(db, ids)
    out = [
        message_out(m, u, f, aggs.get(m.id), comps.get(m.id), None, threads.get(m.id))
        for m, u, f in rows
    ]
    out.sort(key=lambda mo: mo.thread_last_at or mo.created_at, reverse=True)
    return out


@router.get("/messages/{root_id}/thread", response_model=list[MessageOut])
async def list_thread(
    root_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    """The root message followed by every reply in its thread, oldest first."""
    root = await db.get(Message, root_id)
    if root is None or root.deleted_at is not None:
        raise HTTPException(404, "Thread not found")
    # If a reply id was passed, resolve to its actual root.
    if root.thread_root_id is not None:
        root = await db.get(Message, root.thread_root_id)
        if root is None or root.deleted_at is not None:
            raise HTTPException(404, "Thread not found")
    await require_member(db, root.channel_id, user)
    rows = (
        await db.execute(
            select(Message, User, File)
            .join(User, User.id == Message.sender_id)
            .outerjoin(File, File.id == Message.file_id)
            .where(
                Message.deleted_at.is_(None),
                (Message.id == root.id) | (Message.thread_root_id == root.id),
            )
            .order_by(Message.created_at, Message.id)
        )
    ).all()
    msgs = [m for m, _, _ in rows]
    ids = [m.id for m in msgs]
    aggs = await reactions_for(db, ids)
    comps = await completions_for(db, ids)
    replies = await reply_previews_for(db, msgs)
    previews = await previews_for(db, msgs)
    return [
        message_out(m, u, f, aggs.get(m.id), comps.get(m.id), replies.get(m.id),
                    None, previews.get(m.id))
        for m, u, f in rows
    ]


async def _broadcast_thread_meta(
    db: AsyncSession, channel_id: uuid.UUID, root_id: uuid.UUID
) -> None:
    meta = (await thread_meta_for(db, [root_id])).get(
        root_id, {"count": 0, "last_at": None, "repliers": []}
    )
    await broadcast(
        db,
        channel_id,
        {
            "type": "thread.updated",
            "channel_id": str(channel_id),
            "root_id": str(root_id),
            "reply_count": meta["count"],
            "thread_last_at": jsonable_encoder(meta["last_at"]),
            "thread_repliers": jsonable_encoder(meta["repliers"]),
        },
    )


async def _notify_level(
    db: AsyncSession, channel: Channel, user_id: uuid.UUID
) -> str:
    pref = await db.get(ChannelNotifyPref, (channel.id, user_id))
    if pref:
        return pref.level
    return "all"


async def _notify_for_message(
    db: AsyncSession, channel: Channel, sender: User, content: str
) -> None:
    snippet = content[:200] if content else "(attachment)"
    if channel.is_dm:
        other_id = await db.scalar(
            select(ChannelMember.user_id).where(
                ChannelMember.channel_id == channel.id,
                ChannelMember.user_id != sender.id,
            )
        )
        # Online users see the message arrive live; notify only the absent.
        if (
            other_id
            and not manager.is_online(other_id)
            and await _notify_level(db, channel, other_id) != "muted"
        ):
            await notify_user(
                db,
                other_id,
                "dm",
                f"💬 New message from {sender.display_name}",
                snippet,
                {"channel_id": str(channel.id)},
            )
        return
    mentioned = set(re.findall(r"@([a-z0-9_]{3,32})", content.lower()))
    # Walk members, honoring each one's per-channel level: muted → nothing,
    # mentions → only when named, all → every message.
    rows = (
        await db.execute(
            select(User.id, User.username, ChannelNotifyPref.level)
            .join(ChannelMember, ChannelMember.user_id == User.id)
            .outerjoin(
                ChannelNotifyPref,
                and_(
                    ChannelNotifyPref.channel_id == channel.id,
                    ChannelNotifyPref.user_id == User.id,
                ),
            )
            .where(
                ChannelMember.channel_id == channel.id,
                User.disabled == False,  # noqa: E712
                User.id != sender.id,
            )
        )
    ).all()
    for uid, uname, level in rows:
        level = level or "all"
        if level == "muted":
            continue
        if uname.lower() in mentioned:
            await notify_user(
                db,
                uid,
                "mention",
                f"💬 {sender.display_name} mentioned you in #{channel.name}",
                snippet,
                {"channel_id": str(channel.id)},
            )
        elif level == "all":
            # Live ping only — don't fill the persistent 🔔 inbox with chatter.
            await notify_user(
                db,
                uid,
                "channel",
                f"💬 {sender.display_name} in #{channel.name}",
                snippet,
                {"channel_id": str(channel.id)},
                inbox=False,
            )


@router.post(
    "/channels/{channel_id}/messages", response_model=MessageOut, status_code=201
)
async def send_message(
    channel_id: uuid.UUID,
    body: MessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    channel = await require_member(db, channel_id, user)
    if body.id is not None:
        # Idempotent offline send: replaying the same client id returns the
        # already-created message instead of duplicating it.
        existing = await db.get(Message, body.id)
        if existing is not None:
            if existing.sender_id != user.id or existing.channel_id != channel_id:
                raise HTTPException(409, "Message id already exists")
            ex_file = await db.get(File, existing.file_id) if existing.file_id else None
            aggs = (await reactions_for(db, [existing.id])).get(existing.id)
            comps = (await completions_for(db, [existing.id])).get(existing.id)
            reply = (await reply_previews_for(db, [existing])).get(existing.id)
            thr = (await thread_meta_for(db, [existing.id])).get(existing.id)
            prev = (await previews_for(db, [existing])).get(existing.id)
            return message_out(existing, user, ex_file, aggs, comps, reply, thr, prev)
    content = body.content.strip()
    file = None
    if body.file_id is not None:
        file = await db.get(File, body.file_id)
        if file is None or file.channel_id != channel_id or file.uploader_id != user.id:
            raise HTTPException(400, "Invalid file attachment")
        if await db.scalar(select(Message.id).where(Message.file_id == file.id)):
            raise HTTPException(400, "File is already attached to a message")
    if not content and file is None:
        raise HTTPException(400, "Message cannot be empty")
    if body.reply_to_id is not None:
        parent = await db.get(Message, body.reply_to_id)
        if parent is None or parent.deleted_at is not None or parent.channel_id != channel_id:
            raise HTTPException(400, "Invalid reply target")
    thread_root_id = None
    thread_root = None
    if body.thread_root_id is not None:
        thread_root = await db.get(Message, body.thread_root_id)
        if (
            thread_root is None
            or thread_root.deleted_at is not None
            or thread_root.channel_id != channel_id
        ):
            raise HTTPException(400, "Invalid thread target")
        # Flatten to one level: a reply to a reply joins the same root.
        thread_root_id = thread_root.thread_root_id or thread_root.id
    message = Message(
        channel_id=channel_id,
        sender_id=user.id,
        content=content,
        file_id=file.id if file else None,
        reply_to_id=body.reply_to_id,
        thread_root_id=thread_root_id,
    )
    if body.id is not None:
        message.id = body.id
    db.add(message)
    await db.flush()
    await record_change(db, message)
    reply = (await reply_previews_for(db, [message])).get(message.id)
    out = message_out(message, user, file, None, None, reply)
    await broadcast(
        db, channel_id, {"type": "message.created", "message": jsonable_encoder(out)}
    )
    schedule_previews(message.id, channel_id, content)
    if thread_root_id is not None:
        await _broadcast_thread_meta(db, channel_id, thread_root_id)
        # Notify everyone in the thread (its author + prior repliers), except
        # the sender and anyone @mentioned here (they get a mention instead).
        participants = await thread_participant_ids(db, thread_root_id, user.id)
        if participants:
            mentioned = set(re.findall(r"@([a-z0-9_]{3,32})", content.lower()))
            prows = (
                await db.execute(
                    select(User.id, User.username, ChannelNotifyPref.level)
                    .outerjoin(
                        ChannelNotifyPref,
                        and_(
                            ChannelNotifyPref.channel_id == channel_id,
                            ChannelNotifyPref.user_id == User.id,
                        ),
                    )
                    .where(
                        User.id.in_(participants),
                        User.disabled == False,  # noqa: E712
                    )
                )
            ).all()
            snippet = content[:200] if content else "(attachment)"
            for uid, uname, level in prows:
                if level == "muted":
                    continue
                if uname.lower() in mentioned:
                    continue
                await notify_user(
                    db,
                    uid,
                    "thread",
                    f"💬 {user.display_name} replied in a thread",
                    snippet,
                    {"channel_id": str(channel_id), "root_id": str(thread_root_id)},
                )
    await _notify_for_message(db, channel, user, content)
    return out


@router.post("/messages/{message_id}/checkbox", response_model=MessageOut)
async def toggle_checkbox(
    message_id: uuid.UUID,
    body: CheckboxIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    """Any channel member may flip a `[ ]`/`[x]` task line — a deliberate,
    narrow exception to sender-only editing: shared task lists are the point.
    Only the checkbox state can change, never the text."""
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    lines = message.content.split("\n")
    if body.line >= len(lines):
        raise HTTPException(400, "No such line")
    if not re.match(r"^\[( |x)\] ", lines[body.line], re.IGNORECASE):
        raise HTTPException(400, "Not a task line")
    lines[body.line] = ("[x] " if body.checked else "[ ] ") + lines[body.line][4:]
    message.content = "\n".join(lines)
    # Deliberately NOT marking edited_at — checking a box isn't an edit.
    existing = await db.get(TaskCompletion, (message_id, body.line))
    if body.checked and existing is None:
        db.add(TaskCompletion(message_id=message_id, line=body.line, cleared_by=user.id))
    elif not body.checked and existing is not None:
        await db.delete(existing)
    await db.flush()
    await record_change(db, message)
    sender = await db.get(User, message.sender_id)
    file = await db.get(File, message.file_id) if message.file_id else None
    aggs = (await reactions_for(db, [message.id])).get(message.id)
    comps = (await completions_for(db, [message.id])).get(message.id)
    out = message_out(message, sender, file, aggs, comps)
    await broadcast(
        db,
        message.channel_id,
        {"type": "message.updated", "message": jsonable_encoder(out)},
    )
    return out


@router.get("/channels/{channel_id}/pins", response_model=list[MessageOut])
async def list_pins(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    await require_member(db, channel_id, user)
    rows = (
        await db.execute(
            select(Message, User)
            .join(User, User.id == Message.sender_id)
            .where(
                Message.channel_id == channel_id,
                Message.deleted_at.is_(None),
                Message.pinned_at.is_not(None),
            )
            .order_by(Message.pinned_at.desc())
        )
    ).all()
    msgs = [m for m, _ in rows]
    files = {
        f.id: f for f in (
            await db.scalars(
                select(File).where(File.id.in_([m.file_id for m in msgs if m.file_id]))
            )
        ).all()
    } if any(m.file_id for m in msgs) else {}
    replies = await reply_previews_for(db, msgs)
    return [
        message_out(m, u, files.get(m.file_id), None, None, replies.get(m.id))
        for m, u in rows
    ]


async def _set_pin(
    db: AsyncSession, message_id: uuid.UUID, user: User, pinned: bool
) -> MessageOut:
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    message.pinned_at = utcnow() if pinned else None
    message.pinned_by = user.id if pinned else None
    await record_change(db, message)
    sender = await db.get(User, message.sender_id)
    file = await db.get(File, message.file_id) if message.file_id else None
    aggs = (await reactions_for(db, [message.id])).get(message.id)
    comps = (await completions_for(db, [message.id])).get(message.id)
    reply = (await reply_previews_for(db, [message])).get(message.id)
    out = message_out(message, sender, file, aggs, comps, reply)
    await broadcast(
        db, message.channel_id, {"type": "message.updated", "message": jsonable_encoder(out)}
    )
    await broadcast(
        db, message.channel_id,
        {"type": "pins.changed", "channel_id": str(message.channel_id)},
    )
    return out


@router.post("/messages/{message_id}/pin", response_model=MessageOut)
async def pin_message(
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    return await _set_pin(db, message_id, user, True)


@router.delete("/messages/{message_id}/pin", response_model=MessageOut)
async def unpin_message(
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    return await _set_pin(db, message_id, user, False)


@router.post("/messages/{message_id}/reactions", response_model=list[ReactionAgg])
async def toggle_reaction(
    message_id: uuid.UUID,
    body: ReactionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ReactionAgg]:
    if body.emoji not in ALLOWED_EMOJI:
        raise HTTPException(400, "Unsupported reaction")
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    existing = await db.get(Reaction, (message_id, user.id, body.emoji))
    if existing:
        await db.delete(existing)
    else:
        db.add(Reaction(message_id=message_id, user_id=user.id, emoji=body.emoji))
    await db.flush()
    await record_change(db, message)
    aggs = (await reactions_for(db, [message_id])).get(message_id, [])
    await broadcast(
        db,
        message.channel_id,
        {
            "type": "message.reacted",
            "message_id": str(message_id),
            "channel_id": str(message.channel_id),
            "reactions": jsonable_encoder(aggs),
        },
    )
    return aggs


@router.patch("/messages/{message_id}", response_model=MessageOut)
async def edit_message(
    message_id: uuid.UUID,
    body: MessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
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
    await record_change(db, message)
    file = await db.get(File, message.file_id) if message.file_id else None
    aggs = (await reactions_for(db, [message.id])).get(message.id)
    reply = (await reply_previews_for(db, [message])).get(message.id)
    out = message_out(message, user, file, aggs, None, reply)
    await broadcast(
        db,
        message.channel_id,
        {"type": "message.updated", "message": jsonable_encoder(out)},
    )
    schedule_previews(message.id, message.channel_id, content)
    return out


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    message = await db.get(Message, message_id)
    if message is None or message.deleted_at is not None:
        raise HTTPException(404, "Message not found")
    await require_member(db, message.channel_id, user)
    if message.sender_id != user.id:
        # Channel owners/admins may remove webhook-posted messages.
        member = await db.get(ChannelMember, (message.channel_id, user.id))
        can_manage = user.is_admin or (member and member.role == "owner")
        if not (message.webhook_name and can_manage):
            raise HTTPException(403, "You can only delete your own messages")
    message.deleted_at = utcnow()
    if message.file_id:
        file = await db.get(File, message.file_id)
        message.file_id = None
        if file:
            await db.delete(file)
            (Path(settings.upload_dir) / str(file.id)).unlink(missing_ok=True)
    await record_change(db, message)
    await broadcast(
        db,
        message.channel_id,
        {
            "type": "message.deleted",
            "message": {"id": str(message.id), "channel_id": str(message.channel_id)},
        },
    )
    if message.thread_root_id is not None:
        await db.flush()
        await _broadcast_thread_meta(db, message.channel_id, message.thread_root_id)
