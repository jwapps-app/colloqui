import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import File, User
from ..schemas import FileOut
from .channels import require_member

router = APIRouter(prefix="/api/v1", tags=["files"])

# Only types that are safe to render in a browser are served inline;
# everything else downloads as an attachment (never executes as a page).
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@router.post("/channels/{channel_id}/files", response_model=FileOut, status_code=201)
async def upload_file(
    channel_id: uuid.UUID,
    upload: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> File:
    await require_member(db, channel_id, user)
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    file_id = uuid.uuid4()
    # Blobs are stored under our own UUID — client filenames never touch paths.
    dest = Path(settings.upload_dir) / str(file_id)
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(256 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        413, f"File exceeds the {settings.max_file_size_mb} MB limit"
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "Empty file")

    content_type = (upload.content_type or "").split(";")[0].strip()[:100]
    filename = Path(upload.filename or "").name[:128]
    record = File(
        id=file_id,
        channel_id=channel_id,
        uploader_id=user.id,
        filename=filename or "file",
        content_type=content_type or "application/octet-stream",
        size_bytes=size,
    )
    db.add(record)
    return record


@router.get("/files/{file_id}")
async def download_file(
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    record = await db.get(File, file_id)
    if record is None:
        raise HTTPException(404, "File not found")
    await require_member(db, record.channel_id, user)
    path = Path(settings.upload_dir) / str(record.id)
    if not path.is_file():
        raise HTTPException(404, "File not found")
    disposition = "inline" if record.content_type in INLINE_TYPES else "attachment"
    return FileResponse(
        path,
        media_type=record.content_type,
        filename=record.filename,
        content_disposition_type=disposition,
    )
