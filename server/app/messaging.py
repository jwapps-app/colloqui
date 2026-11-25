"""Messages: the one place that knows how a message is turned into its API
shape, and what has to happen when one is posted or changed.

Route handlers stay thin. They check access, then call:

  serialize_messages()   the only way a MessageOut is assembled
  post_message()         everything that follows from a new message
  publish_update()       everything that follows from an edit, pin or checkbox
  publish_reactions()    ...from a reaction
  publish_deletion()     ...from a delete

Each publish_* function records the change for the sync feed, queues the
socket and integration events for after the commit (see db.after_commit), and
commits. Which side effects each kind of change has is decided here, in one
table of calls, instead of handler by handler.
"""
import logging
import uuid
from enum import Flag, auto

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import background, events, links, storage
from .db import SessionLocal, after_commit, commit
from .membership import channel_member_ids
from .models import (
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
from .notify import notify_user
from .schemas import (
    FileOut,
    LinkPreviewOut,
    MessageOut,
    ReactionAgg,
    ReplyPreview,
    UserOut,
)
from .security import hash_token
from .vocab import ALLOWED_EMOJI, DEFAULT_NOTIFY_LEVEL, NotifyLevel, mentioned_usernames
from .ws import manager

log = logging.getLogger("messages")

# Stable sentinel identity for webhook-posted messages (never a real user, so
# no one sees edit/delete-as-self on them).
WEBHOOK_SENDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# How much of a body a notification or push carries.
_SNIPPET_CHARS = 200

# ============================================================================
# Serialization
# ============================================================================


class Part(Flag):
    """The optional pieces of a serialized message. Each costs one query for
    the whole batch, so a feed asks only for what it shows."""

    REACTIONS = auto()
    COMPLETIONS = auto()   # when each ticked task line was checked
    REPLY = auto()         # preview of the message this one replies to
    THREAD = auto()        # reply count, last reply, a few repliers (roots)
    PREVIEWS = auto()      # cached link cards

    # The named choices. A feed uses one of these, never an ad hoc mix.
    FULL = REACTIONS | COMPLETIONS | REPLY | THREAD | PREVIEWS
    # Inside a thread view nothing carries a thread summary of its own.
    IN_THREAD = REACTIONS | COMPLETIONS | REPLY | PREVIEWS
    # Thread lists show the root as a digest: counts and faces, no quoting.
    DIGEST = REACTIONS | COMPLETIONS | THREAD
    # Pin lists, and a message at the moment it is first posted.
    REPLY_ONLY = REPLY
    # Search hits and the cross-channel pins feed: text, sender and file only.
    BARE = 0


def message_out(
    message: Message,
    sender: User,
    *,
    file: File | None = None,
    reactions: list[ReactionAgg] | None = None,
    cleared: dict[str, object] | None = None,
    reply: ReplyPreview | None = None,
    thread: dict | None = None,
    previews: list[LinkPreviewOut] | None = None,
) -> MessageOut:
    """Build one MessageOut from already-loaded pieces. Call
    serialize_messages() instead unless you are it."""
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


async def serialize_messages(
    db: AsyncSession,
    rows: list[tuple[Message, User, File | None]],
    *,
    include: Part = Part.FULL,
    tombstone_text: str = "",
) -> list[MessageOut]:
    """Serialize `(message, sender, file)` rows, in order, loading each
    requested part once for the whole batch.

    A deleted message always comes out as a tombstone: its ids and
    `deleted_at`, with `tombstone_text` in place of the body and nothing else.
    The original text must never ride along (it is hidden from history, and a
    member who joined later could otherwise read it), so no caller can get
    this wrong by forgetting a filter.
    """
    rows = list(rows)
    live = [m for m, _, _ in rows if m.deleted_at is None]
    ids = [m.id for m in live]
    reactions = await _reactions_for(db, ids) if Part.REACTIONS in include else {}
    cleared = await _completions_for(db, ids) if Part.COMPLETIONS in include else {}
    replies = await _reply_previews_for(db, live) if Part.REPLY in include else {}
    threads = await thread_meta_for(db, ids) if Part.THREAD in include else {}
    previews = await _previews_for(db, live) if Part.PREVIEWS in include else {}
    out: list[MessageOut] = []
    for m, sender, file in rows:
        if m.deleted_at is not None:
            out.append(
                message_out(m, sender).model_copy(update={"content": tombstone_text})
            )
            continue
        out.append(
            message_out(
                m,
                sender,
                file=file,
                reactions=reactions.get(m.id),
                cleared=cleared.get(m.id),
                reply=replies.get(m.id),
                thread=threads.get(m.id),
                previews=previews.get(m.id),
            )
        )
    return out


async def serialize_message(
    db: AsyncSession,
    message: Message,
    sender: User,
    file: File | None,
    *,
    include: Part = Part.FULL,
) -> MessageOut:
    """One message, complete by default. Mutation handlers broadcast this:
    clients replace the whole rendered message with it, so a partial one would
    wipe reply counts and link cards from the UI until reload."""
    return (await serialize_messages(db, [(message, sender, file)], include=include))[0]


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


async def _reply_previews_for(
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


def _card(preview: LinkPreview) -> LinkPreviewOut:
    return LinkPreviewOut(
        url=preview.url,
        title=preview.title,
        description=preview.description,
        site_name=preview.site_name,
    )


async def _previews_for(
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
            _card(by_hash[h]) for h in (hash_token(u) for u in urls) if h in by_hash
        ]
        if cards:
            out[mid] = cards
    return out


async def _completions_for(
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


async def _reactions_for(
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


# ============================================================================
# The sync change-log
# ============================================================================


async def record_change(db: AsyncSession, message: Message) -> None:
    """Stamp a message into the offline-sync change-log (upsert, one row per
    message) with a fresh monotonic seq. Call after any change to a message's
    rendered state (create/edit/delete/checkbox/pin/reaction).

    Sequence values are allocated in transaction order, but transactions commit
    in any order, so a poller could advance past a not-yet-committed earlier
    seq and never see it. The transaction-scoped advisory lock below is held
    from allocation until this transaction commits, which serialises the
    allocate-then-commit window: seq order IS commit order, so `seq > cursor`
    is gap-free. Writers block here only for the brief tail of another write.
    That is also why every function below commits as soon as the change is
    recorded instead of leaving it to the end of the request."""
    await db.execute(text("SELECT pg_advisory_xact_lock(7331002)"))
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


# ============================================================================
# Posting
# ============================================================================


async def post_message(
    db: AsyncSession,
    channel: Channel,
    sender: User,
    content: str,
    *,
    file_id: uuid.UUID | None = None,
    reply_to_id: uuid.UUID | None = None,
    thread_root_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    webhook_name: str | None = None,
    fetch_previews: bool = True,
) -> MessageOut:
    """Post a message into `channel` as `sender` and do everything that
    follows: sync change-log, live event, integration event, link previews,
    thread summary, notifications. The caller has already checked that the
    sender may post here.

    `client_id` makes the send idempotent (offline replay): posting the same id
    again returns the message it already created. `webhook_name` marks a
    message posted by an incoming webhook, shown under that name.
    """
    if client_id is not None:
        existing = await db.get(Message, client_id)
        if existing is not None:
            if existing.sender_id != sender.id or existing.channel_id != channel.id:
                raise HTTPException(409, "Message id already exists")
            ex_file = await db.get(File, existing.file_id) if existing.file_id else None
            return await serialize_message(db, existing, sender, ex_file)

    content = content.strip()
    file = None
    if file_id is not None:
        file = await db.get(File, file_id)
        if file is None or file.channel_id != channel.id or file.uploader_id != sender.id:
            raise HTTPException(400, "Invalid file attachment")
        if await db.scalar(select(Message.id).where(Message.file_id == file.id)):
            raise HTTPException(400, "File is already attached to a message")
    if not content and file is None:
        raise HTTPException(400, "Message cannot be empty")
    if reply_to_id is not None:
        parent = await db.get(Message, reply_to_id)
        if parent is None or parent.deleted_at is not None or parent.channel_id != channel.id:
            raise HTTPException(400, "Invalid reply target")
    root_id = await _resolve_thread_root(db, channel.id, thread_root_id)

    message = Message(
        channel_id=channel.id,
        sender_id=sender.id,
        content=content,
        file_id=file.id if file else None,
        reply_to_id=reply_to_id,
        thread_root_id=root_id,
        webhook_name=webhook_name,
    )
    if client_id is not None:
        message.id = client_id
    db.add(message)
    await db.flush()
    await record_change(db, message)
    if root_id is not None:
        # The root's rendered state (reply count / last reply) changed too, so
        # stamp it into the change-log or sync clients never learn about it.
        root = await db.get(Message, root_id)
        if root is not None:
            await record_change(db, root)

    out = await serialize_message(db, message, sender, file, include=Part.REPLY_ONLY)
    payload = jsonable_encoder(out)
    members = await channel_member_ids(db, channel.id)
    events.publish(db, members, {"type": "message.created", "message": payload})
    events.emit(db, "message.created", payload)
    if fetch_previews:
        after_commit(db, schedule_previews, message.id, channel.id, content)
    if root_id is not None:
        await _publish_thread_meta(db, channel.id, root_id, members)
    # Commit before notifying. The message is now saved and announced even if
    # building someone's notification fails below, and the change-log lock is
    # released before the per-member fan-out.
    await commit(db)

    await _notify(db, channel, sender, content, root_id)
    return out


async def _resolve_thread_root(
    db: AsyncSession, channel_id: uuid.UUID, thread_root_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The live root a thread reply belongs under. Threads are one level deep:
    a reply to a reply joins the same root."""
    if thread_root_id is None:
        return None
    target = await db.get(Message, thread_root_id)
    if (
        target is None
        or target.deleted_at is not None
        or target.channel_id != channel_id
    ):
        raise HTTPException(400, "Invalid thread target")
    root_id = target.thread_root_id or target.id
    if root_id != target.id:
        # Replying via a surviving reply must still land under a LIVE root: a
        # deleted thread accepts no new posts (it stays readable as a
        # tombstone, see the thread listing).
        root = await db.get(Message, root_id)
        if root is None or root.deleted_at is not None:
            raise HTTPException(400, "That thread is no longer available")
    return root_id


async def _publish_thread_meta(
    db: AsyncSession,
    channel_id: uuid.UUID,
    root_id: uuid.UUID,
    members: list[uuid.UUID],
) -> None:
    meta = (await thread_meta_for(db, [root_id])).get(
        root_id, {"count": 0, "last_at": None, "repliers": []}
    )
    events.publish(
        db,
        members,
        {
            "type": "thread.updated",
            "channel_id": str(channel_id),
            "root_id": str(root_id),
            "reply_count": meta["count"],
            "thread_last_at": jsonable_encoder(meta["last_at"]),
            "thread_repliers": jsonable_encoder(meta["repliers"]),
        },
    )


# ============================================================================
# Changing
# ============================================================================


async def publish_update(
    db: AsyncSession,
    message: Message,
    sender: User,
    *,
    tell_integrations: bool,
    refetch_previews: bool = False,
    also: dict | None = None,
) -> MessageOut:
    """A message's rendered state changed (edit, checkbox, pin). Record it,
    announce the complete message, commit.

    `tell_integrations`: whether outgoing webhooks hear `message.updated`.
    Edits and checkbox flips do; pinning does not (it is a view preference of
    the channel, not a change to the message's content).
    `also`: one more socket event for the channel, sent after the update.
    """
    await db.flush()
    await record_change(db, message)
    file = await db.get(File, message.file_id) if message.file_id else None
    out = await serialize_message(db, message, sender, file)
    payload = jsonable_encoder(out)
    members = await channel_member_ids(db, message.channel_id)
    events.publish(db, members, {"type": "message.updated", "message": payload})
    if also is not None:
        events.publish(db, members, also)
    if tell_integrations:
        events.emit(db, "message.updated", payload)
    if refetch_previews:
        after_commit(
            db, schedule_previews, message.id, message.channel_id, message.content
        )
    await commit(db)
    return out


async def publish_reactions(db: AsyncSession, message: Message) -> list[ReactionAgg]:
    """A reaction was added or removed. Clients get just the new tallies (not
    the whole message), and integrations are not told: reactions are chatter."""
    await db.flush()
    await record_change(db, message)
    aggs = (await _reactions_for(db, [message.id])).get(message.id, [])
    events.publish(
        db,
        await channel_member_ids(db, message.channel_id),
        {
            "type": "message.reacted",
            "message_id": str(message.id),
            "channel_id": str(message.channel_id),
            "reactions": jsonable_encoder(aggs),
        },
    )
    await commit(db)
    return aggs


async def publish_deletion(db: AsyncSession, message: Message) -> None:
    """Soft-delete a message: hide it, drop its attachment, tell everyone."""
    message.deleted_at = utcnow()
    if message.file_id:
        file = await db.get(File, message.file_id)
        message.file_id = None
        if file:
            await db.delete(file)
            # The blob goes only once the row's deletion is committed.
            after_commit(db, storage.unlink_blobs, [file.id])
    await db.flush()
    await record_change(db, message)
    members = await channel_member_ids(db, message.channel_id)
    events.publish(
        db,
        members,
        {
            "type": "message.deleted",
            "message": {
                "id": str(message.id),
                "channel_id": str(message.channel_id),
                "thread_root_id": str(message.thread_root_id)
                if message.thread_root_id
                else None,
            },
        },
    )
    events.emit(
        db,
        "message.deleted",
        {"id": str(message.id), "channel_id": str(message.channel_id)},
    )
    if message.thread_root_id is not None:
        await _publish_thread_meta(
            db, message.channel_id, message.thread_root_id, members
        )
    await commit(db)


# ============================================================================
# Notifications for a new message
# ============================================================================


async def _notify(
    db: AsyncSession,
    channel: Channel,
    sender: User,
    content: str,
    thread_root_id: uuid.UUID | None,
) -> None:
    """Who hears about a new message, beyond seeing it arrive live. Every
    recipient is a CURRENT, enabled member of the channel: someone removed
    from a private channel, or disabled, must not keep receiving its text."""
    snippet = content[:_SNIPPET_CHARS] if content else "(attachment)"
    mentioned = mentioned_usernames(content)
    if thread_root_id is not None:
        await _notify_thread(db, channel, sender, snippet, mentioned, thread_root_id)
    if channel.is_dm:
        await _notify_dm(db, channel, sender, snippet)
    else:
        await _notify_channel(db, channel, sender, snippet, mentioned)


def _members_with_level(channel_id: uuid.UUID, sender_id: uuid.UUID, *columns):
    """Query: the channel's enabled members other than the sender, each with
    their per-channel notify level (NULL when they never chose one)."""
    return (
        select(*columns, ChannelNotifyPref.level)
        .join(ChannelMember, ChannelMember.user_id == User.id)
        .outerjoin(
            ChannelNotifyPref,
            and_(
                ChannelNotifyPref.channel_id == channel_id,
                ChannelNotifyPref.user_id == User.id,
            ),
        )
        .where(
            ChannelMember.channel_id == channel_id,
            User.disabled == False,  # noqa: E712
            User.id != sender_id,
        )
    )


async def _notify_thread(
    db: AsyncSession,
    channel: Channel,
    sender: User,
    snippet: str,
    mentioned: set[str],
    root_id: uuid.UUID,
) -> None:
    """Everyone in the thread (its author and prior repliers), except anyone
    @mentioned here: they get a mention instead."""
    participants = (
        select(Message.sender_id)
        .where(
            Message.deleted_at.is_(None),
            (Message.id == root_id) | (Message.thread_root_id == root_id),
        )
        .distinct()
    )
    rows = (
        await db.execute(
            _members_with_level(channel.id, sender.id, User.id, User.username).where(
                User.id.in_(participants)
            )
        )
    ).all()
    for uid, uname, level in rows:
        if level == NotifyLevel.MUTED or uname.lower() in mentioned:
            continue
        await notify_user(
            db,
            uid,
            "thread",
            f"💬 {sender.display_name} replied in a thread",
            snippet,
            {"channel_id": str(channel.id), "root_id": str(root_id)},
        )


async def _notify_dm(
    db: AsyncSession, channel: Channel, sender: User, snippet: str
) -> None:
    """ALL other members (group DMs exist), unless they muted the DM."""
    rows = (
        await db.execute(_members_with_level(channel.id, sender.id, User.id))
    ).all()
    for other_id, level in rows:
        # Online users see the message arrive live; notify only the absent.
        if manager.is_online(other_id):
            continue
        if (level or DEFAULT_NOTIFY_LEVEL) == NotifyLevel.MUTED:
            continue
        await notify_user(
            db,
            other_id,
            "dm",
            f"💬 New message from {sender.display_name}",
            snippet,
            {"channel_id": str(channel.id)},
        )


async def _notify_channel(
    db: AsyncSession,
    channel: Channel,
    sender: User,
    snippet: str,
    mentioned: set[str],
) -> None:
    """Honour each member's per-channel level: muted hears nothing, mentions
    only when named, all hears every message."""
    rows = (
        await db.execute(
            _members_with_level(
                channel.id,
                sender.id,
                User.id,
                User.username,
                User.badge_channel_messages,
            )
        )
    ).all()
    for uid, uname, badge_channel, level in rows:
        level = level or DEFAULT_NOTIFY_LEVEL
        if level == NotifyLevel.MUTED:
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
        elif level == NotifyLevel.ALL:
            # Ordinary channel chatter. By default it lands in the 🔔 inbox and
            # counts toward the badge; users who opt out get just the live ping.
            await notify_user(
                db,
                uid,
                "channel",
                f"💬 {sender.display_name} in #{channel.name}",
                snippet,
                {"channel_id": str(channel.id)},
                inbox=bool(badge_channel),
            )


# ============================================================================
# Link previews (fetched off the request path)
# ============================================================================


def schedule_previews(message_id: uuid.UUID, channel_id: uuid.UUID, content: str) -> None:
    """Fetch link previews in the background, then push them to clients over
    the WebSocket when ready."""
    urls = links.extract_urls(content)
    if urls:
        background.spawn(
            _fetch_and_publish_previews(message_id, channel_id, urls), "link preview"
        )


async def _fetch_and_publish_previews(
    message_id: uuid.UUID, channel_id: uuid.UUID, urls: list[str]
) -> None:
    async with SessionLocal() as db:
        cards: list[LinkPreviewOut] = []
        for url in urls:
            preview = await _ensure_preview(db, url)
            if preview is not None and preview.ok:
                cards.append(_card(preview))
        if cards:
            events.publish(
                db,
                await channel_member_ids(db, channel_id),
                {
                    "type": "message.preview",
                    "message_id": str(message_id),
                    "channel_id": str(channel_id),
                    "previews": jsonable_encoder(cards),
                },
            )
        await commit(db)


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
