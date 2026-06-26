import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_admin_user
from ..models import (
    API_KEY_PREFIX,
    ApiKey,
    Channel,
    ChannelMember,
    EventSubscription,
    File,
    Invite,
    PasswordCredential,
    User,
    WebAuthnCredential,
    utcnow,
)
from ..models import Session as AuthSession
from ..security import hash_password
from ..schemas import (
    AdminChannelOut,
    AdminCreateUserIn,
    AdminUserCreatedOut,
    AdminUserOut,
    AdminUserUpdateIn,
    ApiKeyCreatedOut,
    ApiKeyIn,
    ApiKeyOut,
    EventSubCreatedOut,
    EventSubIn,
    EventSubOut,
    InviteCreatedOut,
    InviteIn,
    InviteOut,
)
from ..security import hash_token, new_invite_code, new_token
from ..ws import manager

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    """Permanently remove an account. Channels/spaces they created are
    reassigned to the acting admin so shared history survives; the user's own
    messages, files, memberships, passkeys, and sessions are deleted."""
    if user_id == admin.id:
        raise HTTPException(400, "You can't delete your own account")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")

    # Preserve channels they created (created_by cascades, which would delete
    # the whole channel + everyone's messages) by reassigning ownership.
    await db.execute(
        update(Channel).where(Channel.created_by == user_id).values(created_by=admin.id)
    )
    # Collect their uploaded file blobs to unlink after the row cascade.
    file_ids = (
        await db.scalars(select(File.id).where(File.uploader_id == user_id))
    ).all()

    await db.delete(target)  # cascades sessions, credentials, memberships, messages…
    await db.commit()

    for file_id in file_ids:
        (Path(settings.upload_dir) / str(file_id)).unlink(missing_ok=True)
    (Path(settings.upload_dir) / "avatars" / str(user_id)).unlink(missing_ok=True)


@router.get("/channels", response_model=list[AdminChannelOut])
async def list_all_channels(
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_admin_user)
) -> list[AdminChannelOut]:
    rows = (
        await db.execute(
            select(Channel, func.count(ChannelMember.user_id))
            .outerjoin(ChannelMember, ChannelMember.channel_id == Channel.id)
            .where(Channel.is_dm == False)  # noqa: E712
            .group_by(Channel.id)
            .order_by(Channel.name)
        )
    ).all()
    return [
        AdminChannelOut(
            id=c.id,
            name=c.name,
            is_private=c.is_private,
            created_at=c.created_at,
            member_count=count,
        )
        for c, count in rows
    ]


@router.get("/users", response_model=list[AdminUserOut])
async def list_all_users(
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_admin_user)
) -> list[AdminUserOut]:
    users = (await db.scalars(select(User).order_by(User.username))).all()
    has_passkey = set(
        (await db.scalars(select(WebAuthnCredential.user_id).distinct())).all()
    )
    has_pw = set((await db.scalars(select(PasswordCredential.user_id))).all())
    out = []
    for u in users:
        item = AdminUserOut.model_validate(u)
        item.has_password = u.id in has_pw
        # "pending" = no way to sign in yet (neither passkey nor password)
        item.pending = u.id not in has_passkey and u.id not in has_pw
        out.append(item)
    return out


@router.post("/users", response_model=AdminUserCreatedOut, status_code=201)
async def create_user(
    body: AdminCreateUserIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AdminUserCreatedOut:
    """Pre-create an account (no passkey yet). The person enrolls their passkey
    later with the returned claim code; admins can assign Spaces/Channels in the
    meantime via the normal member-management tools."""
    username = body.username.lower()
    if await db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "Username is taken")
    user = User(
        username=username, display_name=body.display_name, is_admin=body.is_admin
    )
    db.add(user)
    await db.flush()
    if body.password:
        # Starter password: the person can sign in immediately with username +
        # password, no passkey required.
        db.add(
            PasswordCredential(user_id=user.id, password_hash=hash_password(body.password))
        )
    # Claim code = a recovery-style invite bound to this account, so the person
    # attaches their first passkey to it on sign-in.
    code = new_invite_code()
    invite = Invite(
        code_hash=hash_token(code),
        created_by=admin.id,
        expires_at=utcnow() + timedelta(hours=168),  # 7 days to enroll
        recover_user_id=user.id,
    )
    db.add(invite)
    return AdminUserCreatedOut(
        username=username,
        display_name=body.display_name,
        claim_code=code,
        expires_at=invite.expires_at,
    )


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdateIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> User:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == admin.id and (body.disabled is True or body.is_admin is False):
        raise HTTPException(400, "You can't disable or demote yourself")
    if body.is_admin is not None:
        target.is_admin = body.is_admin
    if body.disabled is not None:
        target.disabled = body.disabled
        if body.disabled:
            # Lock them out everywhere, immediately.
            await db.execute(
                update(AuthSession)
                .where(AuthSession.user_id == target.id)
                .values(revoked=True)
            )
            await manager.disconnect_user(target.id)
    return target


@router.post("/invites", response_model=InviteCreatedOut, status_code=201)
async def create_invite(
    body: InviteIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> InviteCreatedOut:
    recover_user = None
    if body.recover_username:
        recover_user = await db.scalar(
            select(User).where(User.username == body.recover_username.lower())
        )
        if recover_user is None:
            raise HTTPException(404, "No account with that username to recover")
    code = new_invite_code()
    invite = Invite(
        code_hash=hash_token(code),
        created_by=admin.id,
        expires_at=utcnow() + timedelta(hours=body.expires_hours),
        recover_user_id=recover_user.id if recover_user else None,
    )
    db.add(invite)
    await db.flush()
    # The plaintext code exists only in this response — only its hash is stored.
    return InviteCreatedOut(
        id=invite.id,
        code=code,
        expires_at=invite.expires_at,
        recovery_for=recover_user.username if recover_user else None,
    )


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_admin_user)
) -> list[Invite]:
    invites = (
        await db.scalars(select(Invite).order_by(Invite.created_at.desc()))
    ).all()
    return list(invites)


@router.delete("/invites/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(404, "Invite not found")
    if invite.used_by is not None:
        raise HTTPException(400, "Invite already used")
    await db.delete(invite)


# ---- Integration: API keys (machine-to-machine auth) ----


@router.post("/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    body: ApiKeyIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> ApiKeyCreatedOut:
    user = await db.scalar(select(User).where(User.username == body.username.lower()))
    if user is None:
        raise HTTPException(404, "No such user to bind the key to")
    token = API_KEY_PREFIX + new_token()
    key = ApiKey(name=body.name.strip(), user_id=user.id, token_hash=hash_token(token))
    db.add(key)
    await db.flush()
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        user_id=key.user_id,
        created_at=key.created_at,
        last_used_at=None,
        key=token,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_admin_user)
) -> list[ApiKey]:
    rows = await db.scalars(
        select(ApiKey).where(ApiKey.revoked_at.is_(None)).order_by(ApiKey.created_at)
    )
    return list(rows)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    key = await db.get(ApiKey, key_id)
    if key is None or key.revoked_at is not None:
        raise HTTPException(404, "API key not found")
    key.revoked_at = utcnow()


# ---- Integration: outgoing event subscriptions (webhooks out) ----


def _sub_out(sub: EventSubscription) -> EventSubOut:
    return EventSubOut(
        id=sub.id,
        url=sub.url,
        events=sub.events.split(",") if sub.events else None,
        active=sub.active,
        created_at=sub.created_at,
    )


@router.post("/event-subscriptions", response_model=EventSubCreatedOut, status_code=201)
async def create_event_subscription(
    body: EventSubIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> EventSubCreatedOut:
    secret = "whsec_" + new_token()
    sub = EventSubscription(
        url=body.url.strip(),
        secret=secret,
        events=",".join(body.events) if body.events else None,
    )
    db.add(sub)
    await db.flush()
    base = _sub_out(sub)
    return EventSubCreatedOut(**base.model_dump(), secret=secret)


@router.get("/event-subscriptions", response_model=list[EventSubOut])
async def list_event_subscriptions(
    db: AsyncSession = Depends(get_db), admin: User = Depends(get_admin_user)
) -> list[EventSubOut]:
    rows = await db.scalars(
        select(EventSubscription).order_by(EventSubscription.created_at)
    )
    return [_sub_out(s) for s in rows]


@router.delete("/event-subscriptions/{sub_id}", status_code=204)
async def delete_event_subscription(
    sub_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    sub = await db.get(EventSubscription, sub_id)
    if sub is None:
        raise HTTPException(404, "Subscription not found")
    await db.delete(sub)
