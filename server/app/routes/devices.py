from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import DeviceToken, User, utcnow
from ..schemas import DeviceRegisterIn

router = APIRouter(prefix="/api/v1", tags=["devices"])


@router.post("/devices", status_code=204)
async def register_device(
    body: DeviceRegisterIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Register (or re-register) this device's APNs token to the current user.
    Idempotent: re-registering an existing token reassigns it to this user."""
    existing = await db.get(DeviceToken, body.token)
    if existing is not None:
        existing.user_id = user.id
        existing.platform = body.platform
        existing.last_seen_at = utcnow()
    else:
        db.add(
            DeviceToken(token=body.token, user_id=user.id, platform=body.platform)
        )


@router.delete("/devices/{token}", status_code=204)
async def unregister_device(
    token: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Drop this device's token (call on logout)."""
    existing = await db.get(DeviceToken, token)
    if existing is not None and existing.user_id == user.id:
        await db.delete(existing)
