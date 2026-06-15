"""Covers the flows that have regressed before: auth gating, space isolation,
sender-only message edits, shared task checkboxes, counts, and user deletion."""

from tests.conftest import auth


async def _default_space_id(client, token):
    # A registered user is auto-joined to the default space; tests create
    # users directly, so seed a space by registering the first admin path is
    # skipped — instead create a space via the admin API.
    r = await client.get("/api/v1/spaces", headers=auth(token))
    spaces = r.json()
    return spaces[0]["id"] if spaces else None


async def test_health(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_unauthenticated_blocked(client):
    assert (await client.get("/api/v1/channels")).status_code == 401


async def test_admin_creates_space_member_isolated(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, _ = await make_user("bob")

    # Admin creates a space and adds only alice.
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok),
                            json={"name": "Acme"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})

    # Non-admin cannot create spaces.
    assert (await client.post("/api/v1/spaces", headers=auth(a_tok),
                              json={"name": "x"})).status_code == 403

    # Alice sees Acme; Bob does not.
    alice_spaces = [s["name"] for s in (await client.get("/api/v1/spaces", headers=auth(a_tok))).json()]
    bob_spaces = [s["name"] for s in (await client.get("/api/v1/spaces", headers=auth(b_tok))).json()]
    assert "Acme" in alice_spaces
    assert "Acme" not in bob_spaces

    # Bob can't create a channel in Acme or browse it.
    assert (await client.post("/api/v1/channels", headers=auth(b_tok),
            json={"name": "x", "space_id": sp["id"]})).status_code == 403
    assert (await client.get(f"/api/v1/channels/browse?space_id={sp['id']}",
            headers=auth(b_tok))).status_code == 404


async def test_message_edit_delete_sender_only(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "S"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    msg = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": "hi"})).json()

    # Admin (not the sender) cannot edit or delete alice's message.
    assert (await client.patch(f"/api/v1/messages/{msg['id']}", headers=auth(admin_tok),
            json={"content": "x"})).status_code == 403
    assert (await client.delete(f"/api/v1/messages/{msg['id']}",
            headers=auth(admin_tok))).status_code == 403
    # Sender can.
    assert (await client.delete(f"/api/v1/messages/{msg['id']}",
            headers=auth(a_tok))).status_code == 204


async def test_checkbox_any_member_and_completion(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "S"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    msg = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": "[ ] milk"})).json()

    # Bob (not the author) can tick the box, and a completion date is recorded.
    out = (await client.post(f"/api/v1/messages/{msg['id']}/checkbox", headers=auth(b_tok),
           json={"line": 0, "checked": True})).json()
    assert out["content"] == "[x] milk"
    assert out["task_cleared"].get("0")
    # Unchecking clears it.
    out = (await client.post(f"/api/v1/messages/{msg['id']}/checkbox", headers=auth(b_tok),
           json={"line": 0, "checked": False})).json()
    assert out["task_cleared"] == {}


async def test_counts_and_read_state(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "S"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    for i in range(3):
        await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                          json={"content": f"m{i}"})

    def chan(channels):
        return next(c for c in channels if c["id"] == ch["id"])

    b_view = chan((await client.get("/api/v1/channels", headers=auth(b_tok))).json())
    assert b_view["message_count"] == 3 and b_view["unread_count"] == 3
    a_view = chan((await client.get("/api/v1/channels", headers=auth(a_tok))).json())
    assert a_view["unread_count"] == 0  # own messages aren't unread

    await client.post(f"/api/v1/channels/{ch['id']}/read", headers=auth(b_tok))
    b_view = chan((await client.get("/api/v1/channels", headers=auth(b_tok))).json())
    assert b_view["unread_count"] == 0


async def test_reaction_validation(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "S"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    msg = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": "hi"})).json()
    assert (await client.post(f"/api/v1/messages/{msg['id']}/reactions", headers=auth(a_tok),
            json={"emoji": "👍"})).status_code == 200
    assert (await client.post(f"/api/v1/messages/{msg['id']}/reactions", headers=auth(a_tok),
            json={"emoji": "💩"})).status_code == 400


async def test_delete_user_reassigns_channels(client, make_user):
    admin_tok, admin_id = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "S"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()

    # Self-delete blocked; deleting alice keeps her channel (reassigned to admin).
    assert (await client.delete(f"/api/v1/admin/users/{admin_id}",
            headers=auth(admin_tok))).status_code == 400
    assert (await client.delete(f"/api/v1/admin/users/{a_id}",
            headers=auth(admin_tok))).status_code == 204
    admin_channels = (await client.get("/api/v1/admin/channels", headers=auth(admin_tok))).json()
    assert any(c["id"] == ch["id"] for c in admin_channels)


async def test_reminder_rejects_past(client, make_user):
    tok, _ = await make_user("alice")
    assert (await client.post("/api/v1/reminders", headers=auth(tok),
            json={"text": "x", "due_at": "2020-01-01T00:00:00Z"})).status_code == 400


async def test_password_login_and_admin_create(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    # Admin pre-creates a user with a starter password.
    r = await client.post("/api/v1/admin/users", headers=auth(admin_tok),
                          json={"username": "jane", "display_name": "Jane", "password": "hunter2pass"})
    assert r.status_code == 201
    # That user can log in with username + password.
    login = await client.post("/api/v1/auth/login/password",
                              json={"username": "jane", "password": "hunter2pass"})
    assert login.status_code == 200
    jane_tok = login.json()["token"]
    # Wrong password is rejected.
    assert (await client.post("/api/v1/auth/login/password",
            json={"username": "jane", "password": "nope"})).status_code == 401
    # Jane has a password but no passkey, so removing it would lock her out.
    assert (await client.delete("/api/v1/auth/password", headers=auth(jane_tok))).status_code == 400
    # Changing requires the current password.
    assert (await client.post("/api/v1/auth/password", headers=auth(jane_tok),
            json={"password": "brandnew1", "current_password": "wrong"})).status_code == 403
    assert (await client.post("/api/v1/auth/password", headers=auth(jane_tok),
            json={"password": "brandnew1", "current_password": "hunter2pass"})).json() == {"ok": True}


async def test_threads(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "T"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    other = (await client.post("/api/v1/channels", headers=auth(a_tok),
             json={"name": "c2", "space_id": sp["id"]})).json()

    root = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
            headers=auth(a_tok), json={"content": "root"})).json()
    reply = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
             headers=auth(a_tok),
             json={"content": "reply one", "thread_root_id": root["id"]})).json()
    assert reply["thread_root_id"] == root["id"]

    # The timeline shows the root (with a reply count) but NOT the thread reply.
    timeline = (await client.get(f"/api/v1/channels/{ch['id']}/messages",
                headers=auth(a_tok))).json()
    ids = [m["id"] for m in timeline]
    assert root["id"] in ids
    assert reply["id"] not in ids
    assert next(m for m in timeline if m["id"] == root["id"])["reply_count"] == 1

    # The thread view returns the root followed by its replies.
    thread = (await client.get(f"/api/v1/messages/{root['id']}/thread",
              headers=auth(a_tok))).json()
    assert [m["id"] for m in thread] == [root["id"], reply["id"]]

    # Replying to a reply flattens into the same thread (one level deep).
    reply2 = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
              headers=auth(a_tok),
              json={"content": "reply two", "thread_root_id": reply["id"]})).json()
    assert reply2["thread_root_id"] == root["id"]
    thread = (await client.get(f"/api/v1/messages/{root['id']}/thread",
              headers=auth(a_tok))).json()
    assert len(thread) == 3

    # Passing a reply id to the thread endpoint resolves to the real root.
    via_reply = (await client.get(f"/api/v1/messages/{reply['id']}/thread",
                 headers=auth(a_tok))).json()
    assert via_reply[0]["id"] == root["id"]

    # A thread target in a different channel is rejected.
    assert (await client.post(f"/api/v1/channels/{other['id']}/messages",
            headers=auth(a_tok),
            json={"content": "x", "thread_root_id": root["id"]})).status_code == 400

    # Deleting a reply drops the root's reply count back.
    await client.delete(f"/api/v1/messages/{reply2['id']}", headers=auth(a_tok))
    timeline = (await client.get(f"/api/v1/channels/{ch['id']}/messages",
                headers=auth(a_tok))).json()
    assert next(m for m in timeline if m["id"] == root["id"])["reply_count"] == 1


async def test_threads_inbox_and_notifications(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "TI"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    await client.post(f"/api/v1/channels/{ch['id']}/members", headers=auth(a_tok),
                      json={"user_id": str(b_id)})

    # Alice starts a thread; Bob replies.
    root = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
            headers=auth(a_tok), json={"content": "topic"})).json()
    await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(b_tok),
                      json={"content": "bob in", "thread_root_id": root["id"]})

    # Alice (the root author) is notified of Bob's reply.
    a_notifs = (await client.get("/api/v1/notifications", headers=auth(a_tok))).json()
    assert any(n["type"] == "thread" and n["data"].get("root_id") == root["id"] for n in a_notifs)

    # Both Alice (started) and Bob (replied) see the thread in their inbox.
    a_inbox = (await client.get("/api/v1/threads", headers=auth(a_tok))).json()
    b_inbox = (await client.get("/api/v1/threads", headers=auth(b_tok))).json()
    assert [t["root"]["id"] for t in a_inbox] == [root["id"]]
    assert [t["root"]["id"] for t in b_inbox] == [root["id"]]
    assert a_inbox[0]["root"]["reply_count"] == 1
    assert a_inbox[0]["channel_name"] == "c"

    # The per-channel Channel-pane endpoint lists the thread for any member.
    ch_threads = (await client.get(f"/api/v1/channels/{ch['id']}/threads",
                  headers=auth(b_tok))).json()
    assert [r["id"] for r in ch_threads] == [root["id"]]
    assert ch_threads[0]["reply_count"] == 1

    # Now Alice replies again — Bob (a prior participant) gets notified too.
    await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                      json={"content": "thanks bob", "thread_root_id": root["id"]})
    b_notifs = (await client.get("/api/v1/notifications", headers=auth(b_tok))).json()
    assert any(n["type"] == "thread" and n["data"].get("root_id") == root["id"] for n in b_notifs)

    # A user not in the thread has an empty inbox.
    c_tok, _ = await make_user("carol")
    assert (await client.get("/api/v1/threads", headers=auth(c_tok))).json() == []


async def test_thread_expiry(client, make_user):
    import uuid as _uuid
    from datetime import timedelta

    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import Message, utcnow

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "EX"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    root = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
            headers=auth(a_tok), json={"content": "root"})).json()
    reply = (await client.post(f"/api/v1/channels/{ch['id']}/messages",
             headers=auth(a_tok),
             json={"content": "r1", "thread_root_id": root["id"]})).json()

    async def thread_count():
        chans = (await client.get("/api/v1/channels", headers=auth(a_tok))).json()
        return next(c for c in chans if c["id"] == ch["id"])["thread_count"]

    # Fresh thread is visible in both lists and the channel's count.
    assert (await client.get(f"/api/v1/channels/{ch['id']}/threads", headers=auth(a_tok))).json()
    assert (await client.get("/api/v1/threads", headers=auth(a_tok))).json()
    assert await thread_count() == 1

    # Backdate the only reply past the active window → the thread expires.
    async with SessionLocal() as db:
        await db.execute(
            update(Message).where(Message.id == _uuid.UUID(reply["id"]))
            .values(created_at=utcnow() - timedelta(days=8))
        )
        await db.commit()
    assert (await client.get(f"/api/v1/channels/{ch['id']}/threads", headers=auth(a_tok))).json() == []
    assert (await client.get("/api/v1/threads", headers=auth(a_tok))).json() == []
    assert await thread_count() == 0

    # A new reply revives it.
    await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                      json={"content": "r2", "thread_root_id": root["id"]})
    assert (await client.get(f"/api/v1/channels/{ch['id']}/threads", headers=auth(a_tok))).json()
    assert await thread_count() == 1


async def test_global_pins(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "P"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    msg = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": "important"})).json()

    # Nothing pinned yet.
    assert (await client.get("/api/v1/pins", headers=auth(a_tok))).json() == []
    # Pin it → appears in the global pins list with channel context.
    await client.post(f"/api/v1/messages/{msg['id']}/pin", headers=auth(a_tok))
    pins = (await client.get("/api/v1/pins", headers=auth(a_tok))).json()
    assert len(pins) == 1
    assert pins[0]["channel_name"] == "c"
    assert pins[0]["message"]["id"] == msg["id"]
    # Unpin → gone.
    await client.delete(f"/api/v1/messages/{msg['id']}/pin", headers=auth(a_tok))
    assert (await client.get("/api/v1/pins", headers=auth(a_tok))).json() == []


async def test_channel_notify_prefs(client, make_user, monkeypatch):
    # Capture every dispatched WebSocket payload so we can tell a transient
    # "alert" from a persistent inbox "notification".
    from app.ws import manager

    # Record (recipients, payload) for each dispatch so we can check Bob alone.
    events: list[tuple] = []

    async def spy(user_ids, payload):
        events.append(({str(u) for u in user_ids}, payload))

    monkeypatch.setattr(manager, "send_to_users", spy)

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")   # poster
    b_tok, b_id = await make_user("bob")     # recipient
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "N"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    await client.post(f"/api/v1/channels/{ch['id']}/members", headers=auth(a_tok),
                      json={"user_id": str(b_id)})

    bob = str(b_id)

    async def bob_inbox():
        return (await client.get("/api/v1/notifications", headers=auth(b_tok))).json()

    def bob_got(kind):  # was a payload of `kind` dispatched to Bob?
        return any(p["type"] == kind and bob in recips for recips, p in events)

    async def post(text):
        events.clear()
        await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                          json={"content": text})

    # Default 'all': a plain message sends Bob a live alert but no inbox entry.
    await post("hello everyone")
    assert bob_got("alert") and not bob_got("notification")
    assert await bob_inbox() == []

    # An @mention always lands in the bell inbox.
    await post("@bob ping")
    assert bob_got("notification")
    assert any(n["type"] == "mention" for n in await bob_inbox())

    # Level 'mentions': a plain message sends Bob nothing (no alert, no inbox).
    await client.put(f"/api/v1/channels/{ch['id']}/notify", headers=auth(b_tok),
                     json={"level": "mentions"})
    await post("background chatter")
    assert not bob_got("alert") and not bob_got("notification")

    # Muted: even an @mention is silent for Bob.
    await client.put(f"/api/v1/channels/{ch['id']}/notify", headers=auth(b_tok),
                     json={"level": "muted"})
    inbox_before = len(await bob_inbox())
    await post("@bob still there?")
    assert not bob_got("alert") and not bob_got("notification")
    assert len(await bob_inbox()) == inbox_before

    # channel_out reflects the chosen level.
    chans = (await client.get("/api/v1/channels", headers=auth(b_tok))).json()
    assert next(c for c in chans if c["id"] == ch["id"])["notify_level"] == "muted"


async def test_incoming_webhooks(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")   # channel owner
    b_tok, b_id = await make_user("bob")     # plain member
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "W"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    await client.post(f"/api/v1/channels/{ch['id']}/members", headers=auth(a_tok),
                      json={"user_id": str(b_id)})

    # A plain member can't create a webhook; the owner can.
    assert (await client.post(f"/api/v1/channels/{ch['id']}/webhooks", headers=auth(b_tok),
            json={"name": "Backups"})).status_code == 403
    created = (await client.post(f"/api/v1/channels/{ch['id']}/webhooks", headers=auth(a_tok),
               json={"name": "Backups"})).json()
    assert created["url"].split("/hooks/")[0]  # has a base
    token = created["url"].rsplit("/", 1)[-1]

    # Posting to the ingest URL (no auth) creates a message under the webhook name.
    assert (await client.post(f"/hooks/{token}", json={"text": "nightly backup ok"})).status_code == 201
    msgs = (await client.get(f"/api/v1/channels/{ch['id']}/messages", headers=auth(b_tok))).json()
    hook_msg = next(m for m in msgs if m["content"] == "nightly backup ok")
    assert hook_msg["sender"]["display_name"] == "Backups"
    assert hook_msg["sender"]["username"] == "webhook"

    # A per-post name override works.
    await client.post(f"/hooks/{token}", json={"text": "deploy done", "name": "CI"})
    msgs = (await client.get(f"/api/v1/channels/{ch['id']}/messages", headers=auth(b_tok))).json()
    assert next(m for m in msgs if m["content"] == "deploy done")["sender"]["display_name"] == "CI"

    # An unknown token is rejected.
    assert (await client.post("/hooks/not-a-real-token", json={"text": "x"})).status_code == 404

    # The owner can delete a webhook-posted message; a plain member cannot.
    assert (await client.delete(f"/api/v1/messages/{hook_msg['id']}", headers=auth(b_tok))).status_code == 403
    assert (await client.delete(f"/api/v1/messages/{hook_msg['id']}", headers=auth(a_tok))).status_code == 204

    # Webhook can be listed and deleted by the owner.
    hooks = (await client.get(f"/api/v1/channels/{ch['id']}/webhooks", headers=auth(a_tok))).json()
    assert len(hooks) == 1 and "token" not in hooks[0] and "url" not in hooks[0]
    assert (await client.delete(f"/api/v1/webhooks/{created['id']}", headers=auth(a_tok))).status_code == 204
    assert (await client.post(f"/hooks/{token}", json={"text": "after delete"})).status_code == 404


async def test_link_previews(client, make_user):
    from app import links
    from app.db import SessionLocal
    from app.models import LinkPreview
    from app.security import hash_token

    # URL extraction.
    assert links.extract_urls("see https://example.com/x and http://foo.org.") == [
        "https://example.com/x", "http://foo.org",
    ]
    # SSRF guard: private / loopback / link-local / non-http are refused with no fetch.
    assert await links.fetch_metadata("http://localhost:8000/") is None
    assert await links.fetch_metadata("http://127.0.0.1/") is None
    assert await links.fetch_metadata("http://10.0.0.5/") is None
    assert await links.fetch_metadata("http://169.254.169.254/latest/meta-data/") is None
    assert await links.fetch_metadata("ftp://example.com/") is None

    # Read path: a cached preview attaches to a message containing that URL.
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "L"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    url = "https://example.com/article"
    async with SessionLocal() as db:
        db.add(LinkPreview(
            url_hash=hash_token(url), url=url, ok=True,
            title="Example Article", description="A short description", site_name="example.com",
        ))
        await db.commit()
    msg = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": f"check this out {url}"})).json()
    msgs = (await client.get(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok))).json()
    m = next(x for x in msgs if x["id"] == msg["id"])
    assert len(m["link_previews"]) == 1
    assert m["link_previews"][0]["title"] == "Example Article"
    assert m["link_previews"][0]["site_name"] == "example.com"


async def test_device_registration(client, make_user):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import DeviceToken

    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")

    # Register a token for alice.
    assert (await client.post("/api/v1/devices", headers=auth(a_tok),
            json={"token": "TOKENABC", "platform": "ios"})).status_code == 204
    async with SessionLocal() as db:
        dt = await db.get(DeviceToken, "TOKENABC")
        assert dt is not None and str(dt.user_id) == str(a_id)

    # Re-registering the same token to bob reassigns it (upsert — still one row).
    assert (await client.post("/api/v1/devices", headers=auth(b_tok),
            json={"token": "TOKENABC"})).status_code == 204
    async with SessionLocal() as db:
        rows = (await db.scalars(select(DeviceToken).where(DeviceToken.token == "TOKENABC"))).all()
        assert len(rows) == 1 and str(rows[0].user_id) == str(b_id)

    # A non-owner can't delete it; the owner can.
    await client.delete("/api/v1/devices/TOKENABC", headers=auth(a_tok))  # no-op
    async with SessionLocal() as db:
        assert await db.get(DeviceToken, "TOKENABC") is not None
    assert (await client.delete("/api/v1/devices/TOKENABC", headers=auth(b_tok))).status_code == 204
    async with SessionLocal() as db:
        assert await db.get(DeviceToken, "TOKENABC") is None


async def test_push_apns(monkeypatch, make_user):
    import time as _time

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from sqlalchemy import select

    from app import push
    from app.config import settings
    from app.db import SessionLocal
    from app.models import DeviceToken

    # Disabled (and a no-op) until configured.
    assert push.push_enabled() is False

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(settings, "apns_key", pem)
    monkeypatch.setattr(settings, "apns_key_id", "KEY123")
    monkeypatch.setattr(settings, "apns_team_id", "TEAM123")
    monkeypatch.setattr(settings, "apns_topic", "co.jjrrr.colloqui")
    push._jwt_cache["token"] = None
    assert push.push_enabled() is True

    # Provider JWT: valid ES256, carries our kid/team.
    tok = push._provider_jwt(pem, _time.time())
    assert pyjwt.get_unverified_header(tok) == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}
    assert pyjwt.decode(tok, key.public_key(), algorithms=["ES256"])["iss"] == "TEAM123"

    # _deliver posts to every token and prunes the ones APNs rejects (410).
    _, c_id = await make_user("carol")
    async with SessionLocal() as db:
        db.add(DeviceToken(token="LIVE", user_id=c_id, platform="ios"))
        db.add(DeviceToken(token="DEAD", user_id=c_id, platform="ios"))
        await db.commit()

    sent: list[str] = []

    class FakeResp:
        def __init__(self, status): self.status_code = status; self.text = ""
        def json(self): return {"reason": "Unregistered"} if self.status_code == 410 else {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, content=None):
            sent.append(url)
            return FakeResp(410 if url.endswith("/DEAD") else 200)

    monkeypatch.setattr(push.httpx, "AsyncClient", FakeClient)
    await push._deliver(c_id, "hi", "there", {"channel_id": "x"})

    assert any(u.endswith("/LIVE") for u in sent) and any(u.endswith("/DEAD") for u in sent)
    async with SessionLocal() as db:
        left = [t.token for t in (await db.scalars(
            select(DeviceToken).where(DeviceToken.user_id == c_id))).all()]
    assert left == ["LIVE"]  # DEAD was pruned


async def test_sync_feed(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    b_tok, b_id = await make_user("bob")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "SY"})).json()
    for uid in (a_id, b_id):
        await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                          json={"user_id": str(uid)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()
    await client.post(f"/api/v1/channels/{ch['id']}/members", headers=auth(a_tok),
                      json={"user_id": str(b_id)})
    # Private channel: only the creator is enrolled, so bob is NOT a member.
    other = (await client.post("/api/v1/channels", headers=auth(a_tok),
             json={"name": "c2", "space_id": sp["id"], "is_private": True})).json()

    async def bob_sync(since):
        return (await client.get(f"/api/v1/sync?since={since}", headers=auth(b_tok))).json()

    # Nothing yet.
    s = await bob_sync(0)
    assert s["messages"] == [] and s["has_more"] is False
    cur = s["cursor"]

    # A new message shows up as a delta for the member.
    m1 = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
          json={"content": "hello"})).json()
    s = await bob_sync(cur)
    assert [x["id"] for x in s["messages"]] == [m1["id"]]
    cur = s["cursor"]

    # Activity in a channel bob isn't in is filtered out for him…
    await client.post(f"/api/v1/channels/{other['id']}/messages", headers=auth(a_tok),
                      json={"content": "secret"})
    assert (await bob_sync(cur))["messages"] == []
    # …but alice (a member of `other`) does receive it.
    sa = (await client.get(f"/api/v1/sync?since={cur}", headers=auth(a_tok))).json()
    assert any(x["content"] == "secret" for x in sa["messages"])

    # An edit re-surfaces the message with new content.
    await client.patch(f"/api/v1/messages/{m1['id']}", headers=auth(a_tok),
                       json={"content": "edited"})
    s = await bob_sync(cur)
    assert [x["id"] for x in s["messages"]] == [m1["id"]]
    assert s["messages"][0]["content"] == "edited"
    cur = s["cursor"]

    # A delete comes through as a tombstone (deleted_at set).
    await client.delete(f"/api/v1/messages/{m1['id']}", headers=auth(a_tok))
    s = await bob_sync(cur)
    assert [x["id"] for x in s["messages"]] == [m1["id"]]
    assert s["messages"][0]["deleted_at"] is not None


async def test_idempotent_send(client, make_user):
    import uuid as _uuid

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "ID"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()

    cid = str(_uuid.uuid4())
    first = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
             json={"id": cid, "content": "queued offline"})).json()
    assert first["id"] == cid
    # Replaying the same client id returns the same message, doesn't duplicate.
    again = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
             json={"id": cid, "content": "queued offline"})).json()
    assert again["id"] == cid
    msgs = (await client.get(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok))).json()
    assert sum(1 for m in msgs if m["id"] == cid) == 1


async def test_recent_count(client, make_user):
    import uuid as _uuid
    from datetime import timedelta

    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import Message, utcnow

    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "RC"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "c", "space_id": sp["id"]})).json()

    async def view():
        chans = (await client.get("/api/v1/channels", headers=auth(a_tok))).json()
        return next(c for c in chans if c["id"] == ch["id"])

    # Two fresh messages: both count toward total and the 7-day window.
    for t in ("one", "two"):
        await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                          json={"content": t})
    v = await view()
    assert v["message_count"] == 2 and v["recent_count"] == 2

    # Backdate one message past 7 days: total unchanged, recent drops to 1.
    old = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
           json={"content": "ancient"})).json()
    async with SessionLocal() as db:
        await db.execute(update(Message).where(Message.id == _uuid.UUID(old["id"]))
                         .values(created_at=utcnow() - timedelta(days=10)))
        await db.commit()
    v = await view()
    assert v["message_count"] == 3 and v["recent_count"] == 2


async def test_clear_notifications(client, make_user):
    from app.db import SessionLocal
    from app.models import Notification

    a_tok, a_id = await make_user("alice")
    b_tok, _ = await make_user("bob")
    async with SessionLocal() as db:
        db.add(Notification(user_id=a_id, type="mention", title="t1", body="b1"))
        db.add(Notification(user_id=a_id, type="mention", title="t2", body="b2"))
        await db.commit()

    ns = (await client.get("/api/v1/notifications", headers=auth(a_tok))).json()
    assert len(ns) == 2

    # Dismiss one.
    nid = ns[0]["id"]
    assert (await client.delete(f"/api/v1/notifications/{nid}", headers=auth(a_tok))).status_code == 204
    ns = (await client.get("/api/v1/notifications", headers=auth(a_tok))).json()
    assert len(ns) == 1 and ns[0]["id"] != nid

    # Another user can't dismiss alice's (no-op).
    await client.delete(f"/api/v1/notifications/{ns[0]['id']}", headers=auth(b_tok))
    assert len((await client.get("/api/v1/notifications", headers=auth(a_tok))).json()) == 1

    # Clear all.
    assert (await client.delete("/api/v1/notifications", headers=auth(a_tok))).status_code == 204
    assert (await client.get("/api/v1/notifications", headers=auth(a_tok))).json() == []


async def test_counts_exclude_thread_replies(client, make_user):
    admin_tok, _ = await make_user("admin", is_admin=True)
    a_tok, a_id = await make_user("alice")
    sp = (await client.post("/api/v1/spaces", headers=auth(admin_tok), json={"name": "TC"})).json()
    await client.post(f"/api/v1/spaces/{sp['id']}/members", headers=auth(admin_tok),
                      json={"user_id": str(a_id)})
    ch = (await client.post("/api/v1/channels", headers=auth(a_tok),
          json={"name": "paperwork", "space_id": sp["id"]})).json()

    async def counts():
        chans = (await client.get("/api/v1/channels", headers=auth(a_tok))).json()
        c = next(x for x in chans if x["id"] == ch["id"])
        return c["message_count"], c["recent_count"]

    root = (await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
            json={"content": "root"})).json()
    assert await counts() == (1, 1)

    # A thread reply must NOT inflate the channel's message count.
    await client.post(f"/api/v1/channels/{ch['id']}/messages", headers=auth(a_tok),
                      json={"content": "reply", "thread_root_id": root["id"]})
    assert await counts() == (1, 1)

    # Deleting the root orphans the reply — but the channel now reads as empty (0),
    # not "1 but empty" (the reported bug).
    await client.delete(f"/api/v1/messages/{root['id']}", headers=auth(a_tok))
    assert await counts() == (0, 0)
