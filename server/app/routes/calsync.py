"""Private per-user iCalendar feed. The person subscribes to the feed URL once
in Google/Apple/Outlook and their Colloqui reminders appear in their calendar.
The feed is authenticated solely by the unguessable token in the URL, since
calendar apps can't send an Authorization header."""

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Channel, Reminder, User, utcnow
from ..security import new_token

router = APIRouter(tags=["calendar"])


def _feed_url(token: str) -> str:
    return f"{settings.origin}/calendar/{token}.ics"


@router.get("/api/v1/calendar/url")
async def calendar_url(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    if not user.calendar_token:
        user.calendar_token = new_token()
    return {"token": user.calendar_token, "url": _feed_url(user.calendar_token)}


@router.post("/api/v1/calendar/regenerate")
async def calendar_regenerate(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    user.calendar_token = new_token()
    return {"token": user.calendar_token, "url": _feed_url(user.calendar_token)}


def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;")
        .replace(",", "\\,").replace("\n", "\\n")
    )


def _dt(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    """iCalendar requires content lines <= 75 octets, folded with CRLF+space."""
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks, cur, limit = [], "", 75
    for ch in line:
        if len((cur + ch).encode("utf-8")) > limit:
            chunks.append(cur)
            cur, limit = ch, 74  # continuation lines carry a leading space
        else:
            cur += ch
    chunks.append(cur)
    return "\r\n ".join(chunks)


@router.get("/calendar/{token}.ics")
async def calendar_feed(token: str, db: AsyncSession = Depends(get_db)) -> Response:
    user = await db.scalar(select(User).where(User.calendar_token == token))
    if user is None or user.disabled:
        raise HTTPException(404, "Not found")
    reminders = (
        await db.scalars(
            select(Reminder)
            .where(Reminder.user_id == user.id, Reminder.fired_at.is_(None))
            .order_by(Reminder.due_at)
        )
    ).all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Colloqui//Reminders//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Colloqui",
    ]
    stamp = _dt(utcnow())
    for r in reminders:
        channel = await db.get(Channel, r.channel_id) if r.channel_id else None
        where = f"In #{channel.name}" if channel and channel.name else "Colloqui reminder"
        lines += [
            "BEGIN:VEVENT",
            f"UID:reminder-{r.id}@colloqui",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_dt(r.due_at)}",
            f"DTEND:{_dt(r.due_at + timedelta(minutes=30))}",
            _fold(f"SUMMARY:{_esc('⏰ ' + r.text)}"),
            _fold(f"DESCRIPTION:{_esc(where)}"),
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER:PT0M",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return Response(content=body, media_type="text/calendar; charset=utf-8")
