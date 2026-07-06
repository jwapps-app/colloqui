"""Seed a demo dataset for the App Store review account.

Creates an admin user `applereview` (+ a few teammates) and believable, generic
content — spaces, channels, a conversation, a task checklist, a thread, a pin,
and pending reminders — so a reviewer sees a fully-populated app.

Run it INSIDE the server (same env as the app, so DATABASE_URL is set):

    # deployed / local compose (use your api service name)
    docker compose exec api python -m app.seed_demo
    # or in a venv from the server/ dir
    REVIEW_PASSWORD='Review2026!' python -m app.seed_demo

Idempotent: if `applereview` already exists it does nothing. To reseed from
scratch, delete that user first (cascades clean up its content) or use a fresh DB.
"""
import asyncio
import os
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Channel,
    ChannelMember,
    Message,
    PasswordCredential,
    Reminder,
    Space,
    SpaceMember,
    User,
    utcnow,
)
from app.security import hash_password

REVIEW_USERNAME = "applereview"
REVIEW_PASSWORD = os.getenv("REVIEW_PASSWORD", "Review2026!")


async def seed() -> None:
    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.username == REVIEW_USERNAME)):
            print(f"'{REVIEW_USERNAME}' already exists — nothing to do.")
            return

        now = utcnow()

        # --- users -------------------------------------------------------
        review = User(username=REVIEW_USERNAME, display_name="Apple Review", is_admin=True)
        maya = User(username="maya", display_name="Maya Chen")
        devon = User(username="devon", display_name="Devon Ross")
        priya = User(username="priya", display_name="Priya Nair")
        everyone = [review, maya, devon, priya]
        db.add_all(everyone)
        await db.flush()
        db.add(PasswordCredential(user_id=review.id, password_hash=hash_password(REVIEW_PASSWORD)))

        # --- spaces + membership ----------------------------------------
        main = Space(name="Acme Team", is_default=True, position=0, created_by=review.id)
        product = Space(name="Product", is_default=False, position=1, created_by=review.id)
        db.add_all([main, product])
        await db.flush()
        for u in everyone:
            db.add(SpaceMember(space_id=main.id, user_id=u.id,
                               role="manager" if u is review else "member"))
        for u in (review, maya, priya):
            db.add(SpaceMember(space_id=product.id, user_id=u.id, role="member"))

        # --- channels ----------------------------------------------------
        def channel(name, topic, space, members, pos, private=False):
            ch = Channel(name=name, topic=topic, space_id=space.id, is_dm=False,
                         is_private=private, position=pos, created_by=review.id)
            db.add(ch)
            return ch, members

        chans = [
            channel("general", "Team-wide announcements and chatter", main, everyone, 0),
            channel("engineering", "Builds, bugs, and deploys", main, everyone, 1),
            channel("design", "Mockups and design review", product, [review, maya, priya], 0),
            channel("launch-planning", "Q3 launch checklist", product, [review, maya, priya], 1),
        ]
        await db.flush()
        for ch, members in chans:
            for u in members:
                db.add(ChannelMember(channel_id=ch.id, user_id=u.id,
                                     role="owner" if u is review else "member"))

        general = chans[0][0]
        engineering = chans[1][0]
        design = chans[2][0]
        launch = chans[3][0]

        # --- messages ----------------------------------------------------
        # Spaced timestamps so the timeline reads naturally (oldest first).
        clock = now - timedelta(hours=6)

        def msg(channel, sender, content, *, minutes=7, root=None,
                pinned=False, edited=False):
            nonlocal clock
            clock = clock + timedelta(minutes=minutes)
            m = Message(channel_id=channel.id, sender_id=sender.id, content=content,
                        created_at=clock, thread_root_id=root.id if root else None)
            if pinned:
                m.pinned_at = clock
                m.pinned_by = review.id
            if edited:
                m.edited_at = clock + timedelta(minutes=1)
            db.add(m)
            return m

        # general — a normal conversation, plus a pinned welcome
        msg(general, review,
            "Welcome to the team! This channel is for company-wide updates. "
            "Pinned posts have the important stuff. 📌", pinned=True)
        msg(general, maya, "Morning all — coffee machine on the 3rd floor is fixed ☕")
        msg(general, devon, "Legend. Also reminder: **all-hands** is Thursday at 10.")
        msg(general, priya, "Adding it to my calendar now 👍")

        # engineering — formatting + a thread
        root = msg(engineering, devon,
                   "Heads up: I'm deploying `v2.4.0` to staging this afternoon.\n"
                   "Changes:\n- New search backend\n- Faster image loading\n- Bug fixes")
        msg(engineering, maya, "Nice — does the search change touch the API contract?",
            root=root)
        msg(engineering, devon, "Nope, fully backwards compatible. Same endpoints.",
            root=root)
        msg(engineering, review, "Great, thanks for flagging. Ship it 🚀", root=root)
        msg(engineering, maya,
            "Quick question — where did we land on the retry timeout? "
            "Was it `10s` or `30s`?")

        # engineering — a task checklist (open tasks derive from these lines)
        msg(engineering, review,
            "Release checklist for **v2.4.0**:\n"
            "- [x] Run the test suite\n"
            "- [x] Update the changelog\n"
            "- [ ] Bump the version tag\n"
            "- [ ] Notify the support team\n"
            "- [ ] Post release notes")

        # design — with a pin
        msg(design, priya, "First pass on the new onboarding flow is up for review.")
        msg(design, maya,
            "Looks clean. One note: the primary button could use more contrast in "
            "dark mode.", pinned=True)
        msg(design, priya, "Good catch — bumping it a shade. Will repost shortly.")

        # launch-planning — a task list + chatter
        msg(launch, review,
            "Q3 launch tasks:\n"
            "- [x] Finalize pricing\n"
            "- [ ] Draft the announcement post\n"
            "- [ ] Line up beta testers\n"
            "- [ ] Schedule the demo")
        msg(launch, maya, "I can own the announcement post. Draft by Friday?")
        msg(launch, review, "Perfect. Setting a reminder to review it. 🗓️")

        await db.flush()

        # --- reminders (pending; some message-linked) -------------------
        db.add(Reminder(user_id=review.id, text="Review Maya's announcement draft",
                        due_at=now + timedelta(days=2), channel_id=launch.id))
        db.add(Reminder(user_id=review.id, text="Bump the version tag for v2.4.0",
                        due_at=now + timedelta(hours=4), channel_id=engineering.id))
        db.add(Reminder(user_id=review.id, text="Prep slides for Thursday all-hands",
                        due_at=now + timedelta(days=1)))

        await db.commit()

    print("✅ Demo data seeded.")
    print(f"   Username: {REVIEW_USERNAME}")
    print(f"   Password: {REVIEW_PASSWORD}")
    print("   Teammates: maya, devon, priya (no password — content authors only)")


if __name__ == "__main__":
    asyncio.run(seed())
