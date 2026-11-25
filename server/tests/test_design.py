"""Characterization tests for the design refactors: each pins behaviour that a
structural change must not alter (one serializer for channels and messages,
events published only after commit, one home for access rules)."""

from datetime import datetime, timedelta, timezone

from tests.conftest import auth


async def _space(client, admin_tok, *member_ids, name="S"):
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok),
                            json={"name": name})).json()
    for uid in member_ids:
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    return sp


def _normalized(ch: dict) -> dict:
    out = dict(ch)
    out["dm_members"] = sorted(m["id"] for m in ch["dm_members"])
    return out


async def test_single_channel_matches_listing(client, make_user):
    """Every endpoint that returns ONE channel must report exactly what the
    sidebar listing reports for it (counts, role, notify level, DM people)."""
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    c_tok, c_id = await make_user("carol")
    sp = await _space(client, admin_tok, a_id, b_id, c_id)
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
                            json={"name": "general", "space_id": sp["id"]})).json()
    cid = ch["id"]

    # Bob fills the channel: a task list, a pinned message, a thread, chatter.
    async def say(tok, content, **extra):
        r = await client.post(f"/api/v1/channels/{cid}/messages", headers=auth(tok),
                              json={"content": content, **extra})
        assert r.status_code == 201, r.text
        return r.json()

    await say(b_tok, "[ ] buy milk\n[x] done already\n[ ] call home")
    pinned = await say(b_tok, "remember this")
    await client.post(f"/api/v1/messages/{pinned['id']}/pin", headers=auth(b_tok))
    root = await say(b_tok, "thread root")
    await say(b_tok, "a reply", thread_root_id=root["id"])
    await say(a_tok, "mine, never unread for me")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = await client.post("/api/v1/reminders", headers=auth(a_tok),
                          json={"text": "ping", "due_at": due, "channel_id": cid})
    assert r.status_code == 201, r.text

    # PUT /notify returns the single-channel view.
    single = (await client.put(f"/api/v1/channels/{cid}/notify", headers=auth(a_tok),
                               json={"level": "mentions"})).json()
    listing = (await client.get("/api/v1/channels", headers=auth(a_tok))).json()
    listed = next(c for c in listing if c["id"] == cid)
    assert _normalized(single) == _normalized(listed)
    assert single["open_task_count"] == 2
    assert single["pinned_count"] == 1
    assert single["thread_count"] == 1
    assert single["reminder_count"] == 1
    assert single["unread_count"] == 4
    assert single["notify_level"] == "mentions"
    assert single["my_role"] == "owner"

    # 1:1 DM and group DM: the people fields must match the listing too.
    dm = (await client.post("/api/v1/dms", headers=auth(a_tok),
                            json={"user_ids": [str(b_id)]})).json()
    group = (await client.post("/api/v1/dms", headers=auth(a_tok),
                               json={"user_ids": [str(b_id), str(c_id)]})).json()
    listing = {c["id"]: c for c in
               (await client.get("/api/v1/channels", headers=auth(a_tok))).json()}
    assert _normalized(dm) == _normalized(listing[dm["id"]])
    assert _normalized(group) == _normalized(listing[group["id"]])
    assert dm["dm_user"]["id"] == str(b_id) and dm["dm_members"] == []
    assert group["dm_user"] is None and len(group["dm_members"]) == 2


async def test_events_reach_clients_only_after_commit(client, make_user, monkeypatch):
    """A socket event tells clients to refetch. If it is sent before the
    transaction commits, the refetch reads the OLD state (and a failed commit
    leaves clients told about a change that never happened). So at the moment
    any event is delivered, the change it describes must already be visible to
    a different database connection."""
    from sqlalchemy import select

    from app import db as dbmod
    from app.models import Channel, ChannelMember, Message, Space, SpaceMember
    from app.ws import manager

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    c_tok, c_id = await make_user("carol")
    sp = await _space(client, admin_tok, a_id, b_id)

    seen: list[tuple[str, bool]] = []
    probe = {"fn": None}

    async def recording_send(user_ids, payload):
        if probe["fn"] is None:
            return
        async with dbmod.SessionLocal() as other:
            seen.append((payload["type"], bool(await probe["fn"](other))))

    monkeypatch.setattr(manager, "send_to_users", recording_send)

    async def step(expected_type, visible, request):
        seen.clear()
        probe["fn"] = visible
        r = await request
        probe["fn"] = None
        assert r.status_code < 300, r.text
        assert expected_type in [t for t, _ in seen], (expected_type, seen)
        assert all(ok for _, ok in seen), (expected_type, seen)
        return r

    # Create a public channel: the space's other members are told to refetch.
    r = await step(
        "channels.changed",
        lambda db: db.scalar(select(Channel.id).where(Channel.name == "general")),
        client.post("/api/v1/channels", headers=auth(a_tok),
                    json={"name": "general", "space_id": sp["id"]}),
    )
    cid = r.json()["id"]

    await step(
        "channel.updated",
        lambda db: db.scalar(select(Channel.id).where(Channel.name == "renamed")),
        client.patch(f"/api/v1/channels/{cid}", headers=auth(a_tok),
                     json={"name": "renamed"}),
    )
    await step(
        "channels.changed",
        lambda db: db.scalar(select(Space.id).where(Space.name == "Renamed space")),
        client.patch(f"/api/v1/spaces/{sp['id']}", headers=auth(admin_tok),
                     json={"name": "Renamed space"}),
    )
    await step(
        "channels.changed",
        lambda db: db.scalar(select(SpaceMember.user_id).where(
            SpaceMember.space_id == sp["id"], SpaceMember.user_id == c_id)),
        client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                    json={"user_id": str(c_id)}),
    )
    await step(
        "message.created",
        lambda db: db.scalar(select(Message.id).where(Message.content == "hello")),
        client.post(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok),
                    json={"content": "hello"}),
    )
    await step(
        "channels.changed",
        lambda db: db.scalar(select(Channel.id).where(Channel.is_dm == True)),  # noqa: E712
        client.post("/api/v1/dms", headers=auth(a_tok), json={"user_ids": [str(b_id)]}),
    )

    async def bob_is_gone(db):
        row = await db.scalar(select(ChannelMember.user_id).where(
            ChannelMember.channel_id == cid, ChannelMember.user_id == b_id))
        return row is None

    await step(
        "channel.deleted",
        bob_is_gone,
        client.delete(f"/api/v1/channels/{cid}/members/{b_id}", headers=auth(a_tok)),
    )


async def test_rolled_back_work_publishes_nothing(app_module):
    """Anything queued for after the commit is dropped if the transaction
    rolls back instead."""
    from sqlalchemy import text

    from app import db as dbmod

    ran: list[str] = []
    async with dbmod.SessionLocal() as db:
        await db.execute(text("SELECT 1"))  # open a transaction
        dbmod.after_commit(db, ran.append, "rolled back")
        await db.rollback()
        dbmod.after_commit(db, ran.append, "committed")
        assert ran == []          # nothing runs before the commit
        await dbmod.commit(db)
    assert ran == ["committed"]


async def test_a_message_looks_the_same_wherever_it_is_returned(client, make_user, monkeypatch):
    """History, the sync feed, the response to a change and the live socket
    event must all describe a message identically: clients replace the whole
    rendered message with whichever one arrives last."""
    from app.ws import manager

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    sp = await _space(client, admin_tok, a_id, b_id)
    cid = (await client.post("/api/v1/channels", headers=auth(a_tok),
                             json={"name": "general", "space_id": sp["id"]})).json()["id"]

    async def say(tok, content, **extra):
        r = await client.post(f"/api/v1/channels/{cid}/messages", headers=auth(tok),
                              json={"content": content, **extra})
        assert r.status_code == 201, r.text
        return r.json()

    first = await say(a_tok, "the original")
    msg = await say(a_tok, "[ ] one\n[ ] two", reply_to_id=first["id"])
    mid = msg["id"]
    await say(b_tok, "in the thread", thread_root_id=mid)
    await client.post(f"/api/v1/messages/{mid}/reactions", headers=auth(b_tok),
                      json={"emoji": "👍"})
    await client.post(f"/api/v1/messages/{mid}/checkbox", headers=auth(b_tok),
                      json={"line": 0, "checked": True})

    live: list[dict] = []

    async def spy(user_ids, payload):
        if payload.get("type") == "message.updated":
            live.append(payload["message"])

    monkeypatch.setattr(manager, "send_to_users", spy)
    returned = (await client.post(f"/api/v1/messages/{mid}/pin", headers=auth(a_tok))).json()

    history = (await client.get(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok))).json()
    listed = next(m for m in history if m["id"] == mid)
    synced = (await client.get("/api/v1/sync?since=0", headers=auth(a_tok))).json()["messages"]
    from_sync = next(m for m in synced if m["id"] == mid)

    assert returned == listed == from_sync == live[-1]
    # ...and it really is the complete message, not a partial one.
    assert returned["pinned"] is True
    assert returned["reply_count"] == 1
    assert returned["reply_to"]["id"] == first["id"]
    assert returned["reactions"][0]["emoji"] == "👍"
    assert list(returned["task_cleared"]) == ["0"]
    assert returned["content"].startswith("[x] one")


async def test_deleted_text_never_comes_back(client, make_user):
    """Once deleted, a message's text is not returned by any feed, including a
    replay of the original (idempotent) send."""
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = await _space(client, admin_tok, a_id)
    cid = (await client.post("/api/v1/channels", headers=auth(a_tok),
                             json={"name": "general", "space_id": sp["id"]})).json()["id"]
    client_id = "11111111-2222-4333-8444-555555555555"
    body = {"content": "secret plans", "id": client_id}
    r = await client.post(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok), json=body)
    assert r.status_code == 201
    assert (await client.delete(f"/api/v1/messages/{client_id}",
                                headers=auth(a_tok))).status_code == 204

    replay = await client.post(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok), json=body)
    assert replay.json()["deleted_at"] is not None
    feeds = [
        replay.text,
        (await client.get(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok))).text,
        (await client.get("/api/v1/sync?since=0", headers=auth(a_tok))).text,
        (await client.get("/api/v1/search/messages?q=secret", headers=auth(a_tok))).text,
        (await client.get(f"/api/v1/messages/{client_id}/thread", headers=auth(a_tok))).text,
    ]
    assert not any("secret plans" in f for f in feeds)


def test_task_line_grammar():
    """The server reads task lines exactly as the shared cases say. The client
    test (tests/js/tasks.test.mjs) reads the SAME file, so the two grammars
    cannot drift apart unnoticed."""
    import json
    from pathlib import Path

    import pytest

    from app import tasks

    cases = json.loads(
        (Path(__file__).parent / "task_grammar_cases.json").read_text()
    )["cases"]
    for c in cases:
        line = c["line"]
        assert tasks.is_task_line(line) is c["task"], line
        opened = tasks.open_tasks(line)
        if not c["task"]:
            assert opened == [], line
            with pytest.raises(tasks.NotATaskLine):
                tasks.set_checked(line, 0, True)
            continue
        assert bool(opened) is c["open"], line
        if c["open"]:
            assert opened == [(0, c["text"])], line
        # Ticking and clearing changes only the box, never the text.
        assert tasks.set_checked(line, 0, True) == "[x] " + c["text"]
        assert tasks.set_checked(line, 0, False) == "[ ] " + c["text"]

    body = "[ ] buy milk\n[x] done\nplain"
    assert tasks.open_tasks(body) == [(0, "buy milk")]
    assert tasks.set_checked(body, 1, False).split("\n")[1] == "[ ] done"
    with pytest.raises(tasks.NoSuchLine):
        tasks.set_checked(body, 99, True)


async def test_sidebar_task_count_matches_the_task_list(client, make_user):
    """The count is computed in SQL and the list in Python: one grammar, so
    they must agree on awkward bodies too."""
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = await _space(client, admin_tok, a_id)
    cid = (await client.post("/api/v1/channels", headers=auth(a_tok),
                             json={"name": "general", "space_id": sp["id"]})).json()["id"]
    for body in (
        "[ ] real one\n[x] done\n[ ] real two",
        "talking about [ ] boxes inline",
        "note\n [ ] indented, not a task",
        "[ ] third\n\n[ ] fourth",
    ):
        r = await client.post(f"/api/v1/channels/{cid}/messages", headers=auth(a_tok),
                              json={"content": body})
        assert r.status_code == 201
    listed = (await client.get("/api/v1/tasks", headers=auth(a_tok))).json()
    channel = next(c for c in (await client.get("/api/v1/channels", headers=auth(a_tok))).json()
                   if c["id"] == cid)
    assert [t["text"] for t in listed] == ["real one", "real two", "third", "fourth"]
    assert channel["open_task_count"] == len(listed) == 4


async def test_a_response_is_sent_only_after_its_change_is_committed(app_module, make_user):
    """A client acts on a response at once (create a user, then sign in as
    them; create a channel, then reload the list). If the response left before
    the commit, that follow-up could not see the change. So at the moment the
    response STARTS, the change must already be visible to another connection."""
    import json

    from sqlalchemy import select

    from app import db as dbmod
    from app.models import User

    admin_tok, _ = await make_user("admin", is_admin=True)
    body = json.dumps({"username": "newbie", "display_name": "New"}).encode()
    seen: dict = {}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
            async with dbmod.SessionLocal() as other:
                row = await other.scalar(select(User.id).where(User.username == "newbie"))
                seen["committed"] = row is not None

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/admin/users",
        "raw_path": b"/api/v1/admin/users", "query_string": b"", "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {admin_tok}".encode()),
        ],
        "client": ("127.0.0.1", 1), "server": ("test", 80),
    }
    await app_module(scope, receive, send)
    assert seen == {"status": 201, "committed": True}


async def test_client_assets_are_versioned_by_the_server(client):
    """One build id, computed from the files, names every asset URL."""
    health = (await client.get("/healthz")).json()
    build = health["build"]
    assert len(build) == 12

    page = await client.get("/")
    assert page.status_code == 200 and "__BUILD__" not in page.text
    assert f'<meta name="build" content="{build}">' in page.text
    assert f'src="/assets/{build}/app.js"' in page.text
    assert page.headers["cache-control"] == "no-cache"

    current = await client.get(f"/assets/{build}/app.js")
    assert current.status_code == 200
    assert current.headers["cache-control"] == "public, max-age=31536000, immutable"
    module = await client.get(f"/assets/{build}/js/state.js")
    assert module.status_code == 200 and "javascript" in module.headers["content-type"]

    # A stale page asking under an old build id still gets the file, but it
    # must never be pinned in a cache under that URL.
    stale = await client.get("/assets/000000000000/app.js")
    assert stale.status_code == 200
    assert stale.headers["cache-control"] == "no-cache"

    # Fixed-URL files the installed app needs stay at the root, never immutable.
    sw = await client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["cache-control"] == "no-cache"
    assert (await client.get("/assets/" + build + "/../../config.py")).status_code == 404


async def test_meta_lists_the_shared_vocabulary(client, make_user):
    from app import vocab

    tok, _ = await make_user("alice")
    assert (await client.get("/api/v1/meta")).status_code == 401
    m = (await client.get("/api/v1/meta", headers=auth(tok))).json()
    assert m["emoji"] == list(vocab.ALLOWED_EMOJI)
    assert m["notify_levels"] == ["all", "mentions", "muted"]
    assert m["default_notify_level"] == "all"
    assert m["recurrences"] == ["daily", "weekly", "monthly", "yearly"]
