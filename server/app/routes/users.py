import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User, utcnow
from ..schemas import MeOut, UpdateMeIn, UserOut

router = APIRouter(prefix="/api/v1/users", tags=["users"])

AVATAR_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _avatar_path(user_id: uuid.UUID) -> Path:
    return Path(settings.upload_dir) / "avatars" / str(user_id)


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[User]:
    users = (
        await db.scalars(
            select(User).where(User.disabled == False).order_by(User.username)  # noqa: E712
        )
    ).all()
    return list(users)


@router.patch("/me", response_model=MeOut)
async def update_me(
    body: UpdateMeIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.badge_channel_messages is not None:
        user.badge_channel_messages = body.badge_channel_messages
    if body.status is not None:
        # Empty string clears it; otherwise store the trimmed note.
        user.status = body.status.strip() or None
    db.add(user)
    return user


@router.post("/me/avatar", response_model=UserOut)
async def upload_avatar(
    upload: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    content_type = (upload.content_type or "").split(";")[0].strip()
    if content_type not in AVATAR_TYPES:
        raise HTTPException(400, "Use a PNG, JPEG, WebP, or GIF image")
    data = await upload.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(413, "Avatar must be under 2 MB")
    if not data:
        raise HTTPException(400, "Empty file")
    path = _avatar_path(user.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    user.avatar_type = content_type
    user.avatar_at = utcnow()
    db.add(user)
    return user


@router.get("/{user_id}/avatar")
async def get_avatar(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    target = await db.get(User, user_id)
    path = _avatar_path(user_id)
    if target is None or target.avatar_type is None or not path.is_file():
        raise HTTPException(404, "No avatar")
    return FileResponse(
        path,
        media_type=target.avatar_type,
        filename="avatar",
        content_disposition_type="inline",
    )
