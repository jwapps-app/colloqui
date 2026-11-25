from fastapi import APIRouter, Depends

from ..deps import get_current_user
from ..models import User
from ..vocab import ALLOWED_EMOJI, DEFAULT_NOTIFY_LEVEL, NotifyLevel, Recurrence

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict:
    """The closed sets of values the client offers as choices. It reads them
    here instead of carrying its own copy, so adding an emoji, a notification
    level or a recurrence is a one-file change (vocab.py)."""
    return {
        "emoji": list(ALLOWED_EMOJI),
        "notify_levels": [level.value for level in NotifyLevel],
        "default_notify_level": DEFAULT_NOTIFY_LEVEL.value,
        "recurrences": [r.value for r in Recurrence],
    }
