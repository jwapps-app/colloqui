import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..config import settings
from ..db import get_db
from ..deps import get_current_user, rate_limit_auth
from ..models import Invite, PasswordCredential, User, WebAuthnCredential, utcnow
from ..models import Session as AuthSession
from ..schemas import (
    AddPasskeyIn,
    LoginOptionsIn,
    LoginPasswordIn,
    MeOut,
    PasskeyOut,
    RegisterOptionsIn,
    RegisterPasswordIn,
    SessionOut,
    SetPasswordIn,
    TokenOut,
    VerifyIn,
)
from ..security import (
    ExpiringStore,
    b64url_decode,
    b64url_encode,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)

router = APIRouter(
    prefix="/api/v1/auth", tags=["auth"], dependencies=[Depends(rate_limit_auth)]
)

# Pre-computed once at import. A failed password login always verifies against a
# real Argon2 hash (this one when the username/credential doesn't exist) so the
# response time can't reveal whether a username exists (enumeration via timing).
_DUMMY_PASSWORD_HASH = hash_password("colloqui-login-timing-equalizer")

# Short-lived WebAuthn challenges, keyed by an opaque token returned to the
# client between the options and verify steps. Single-process only.
pending_registrations = ExpiringStore(ttl_seconds=settings.challenge_ttl_seconds)
pending_logins = ExpiringStore(ttl_seconds=settings.challenge_ttl_seconds)
pending_passkey_adds = ExpiringStore(ttl_seconds=settings.challenge_ttl_seconds)

REGISTRATION_AUTH_SELECTION = AuthenticatorSelectionCriteria(
    resident_key=ResidentKeyRequirement.REQUIRED,
    user_verification=UserVerificationRequirement.PREFERRED,
)


async def _issue_session(db: AsyncSession, user: User, request: Request) -> str:
    token = new_token()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(days=settings.session_ttl_days),
            user_agent=(request.headers.get("user-agent") or "")[:255],
        )
    )
    return token


async def _valid_invite(db: AsyncSession, code: str) -> Invite | None:
    invite = await db.scalar(select(Invite).where(Invite.code_hash == hash_token(code)))
    if invite is None or invite.used_by is not None or invite.expires_at < utcnow():
        return None
    return invite


def _registration_payload(credential: dict, challenge: bytes):
    parsed = parse_registration_credential_json(json.dumps(credential))
    return verify_registration_response(
        credential=parsed,
        expected_challenge=challenge,
        expected_origin=settings.origin,
        expected_rp_id=settings.rp_id,
        require_user_verification=False,
    )


async def _resolve_registration(
    db: AsyncSession, username: str, invite_code: str | None
) -> tuple[bool, "Invite | None", "User | None"]:
    """Validate the invite for a registration attempt (passkey or password).
    Returns (is_first_user, invite, recover_user)."""
    is_first_user = (await db.scalar(select(func.count()).select_from(User))) == 0
    invite = None
    recover_user = None
    if not is_first_user:
        if not invite_code:
            raise HTTPException(403, "An invite code is required")
        invite = await _valid_invite(db, invite_code)
        if invite is None:
            raise HTTPException(403, "Invalid or expired invite code")
        if invite.recover_user_id:
            recover_user = await db.get(User, invite.recover_user_id)
            if recover_user is None or recover_user.disabled:
                raise HTTPException(403, "Invalid or expired invite code")
            if recover_user.username != username:
                raise HTTPException(
                    403, "This recovery invite is for a different username"
                )
    if recover_user is None:
        if await db.scalar(select(User.id).where(User.username == username)):
            raise HTTPException(409, "Username is taken")
    return is_first_user, invite, recover_user


@router.post("/register/options")
async def register_options(
    body: RegisterOptionsIn, db: AsyncSession = Depends(get_db)
) -> dict:
    username = body.username.lower()
    is_first_user, invite, recover_user = await _resolve_registration(
        db, username, body.invite_code
    )

    user_id = recover_user.id if recover_user else uuid.uuid4()
    display_name = recover_user.display_name if recover_user else body.display_name
    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_id=user_id.bytes,
        user_name=username,
        user_display_name=display_name,
        authenticator_selection=REGISTRATION_AUTH_SELECTION,
    )
    reg_token = pending_registrations.put(
        {
            "challenge": options.challenge,
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "invite_id": invite.id if invite else None,
            "is_admin": is_first_user,
            "recover": recover_user is not None,
        }
    )
    return {"reg_token": reg_token, "options": json.loads(options_to_json(options))}


@router.post("/register/verify", response_model=TokenOut)
async def register_verify(
    body: VerifyIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    pending = pending_registrations.pop(body.token)
    if pending is None:
        raise HTTPException(400, "Registration session expired, start over")

    try:
        verification = _registration_payload(body.credential, pending["challenge"])
    except Exception:
        raise HTTPException(400, "Passkey verification failed")

    # Re-check invariants that may have raced since the options call.
    invite = None
    if pending["invite_id"]:
        invite = await db.get(Invite, pending["invite_id"])
        if invite is None or invite.used_by is not None or invite.expires_at < utcnow():
            raise HTTPException(403, "Invite is no longer valid")
    elif pending["is_admin"]:
        if await db.scalar(select(func.count()).select_from(User)):
            raise HTTPException(403, "Server is already initialized")

    if pending["recover"]:
        user = await db.get(User, pending["user_id"])
        if user is None or user.disabled:
            raise HTTPException(403, "Account unavailable")
        # The old device may be lost or stolen — invalidate everything it held.
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id)
            .values(revoked=True)
        )
    else:
        if await db.scalar(select(User.id).where(User.username == pending["username"])):
            raise HTTPException(409, "Username is taken")
        user = User(
            id=pending["user_id"],
            username=pending["username"],
            display_name=pending["display_name"],
            is_admin=pending["is_admin"],
        )
        db.add(user)

    # Flush the user row before anything that references it: without
    # relationship() mappings SQLAlchemy doesn't order writes across tables,
    # so the credential/invite/session rows would race the user INSERT.
    await db.flush()

    db.add(
        WebAuthnCredential(
            id=verification.credential_id,
            user_id=user.id,
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
            last_used_at=utcnow(),
        )
    )
    if invite:
        invite.used_by = user.id
        invite.used_at = utcnow()

    if not pending["recover"]:
        # Every new user joins the default space (and its public channels).
        from .spaces import add_to_space, ensure_default_space

        space = await ensure_default_space(db, user.id)
        await add_to_space(
            db, space.id, user.id, "manager" if user.is_admin else "member"
        )

    token = await _issue_session(db, user, request)
    # Commit before responding: a deferred commit failure would roll back the
    # account *after* the client already saved its passkey and got a 200.
    await db.commit()
    return TokenOut(token=token, user=MeOut.model_validate(user))


@router.post("/login/options")
async def login_options(body: LoginOptionsIn, db: AsyncSession = Depends(get_db)) -> dict:
    allow_credentials: list[PublicKeyCredentialDescriptor] = []
    if body.username:
        user = await db.scalar(select(User).where(User.username == body.username.lower()))
        if user and not user.disabled:
            creds = await db.scalars(
                select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
            )
            allow_credentials = [PublicKeyCredentialDescriptor(id=c.id) for c in creds]
        # Unknown usernames still get (unusable) options — don't reveal who exists.

    options = generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=allow_credentials or None,  # None => discoverable passkeys
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    login_token = pending_logins.put({"challenge": options.challenge})
    return {"login_token": login_token, "options": json.loads(options_to_json(options))}


@router.post("/login/verify", response_model=TokenOut)
async def login_verify(
    body: VerifyIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    pending = pending_logins.pop(body.token)
    if pending is None:
        raise HTTPException(400, "Login session expired, try again")

    try:
        credential = parse_authentication_credential_json(json.dumps(body.credential))
    except Exception:
        raise HTTPException(400, "Malformed credential")

    db_cred = await db.get(WebAuthnCredential, credential.raw_id)
    if db_cred is None:
        raise HTTPException(401, "This passkey isn't registered here (it may be left over from a failed registration — try registering again, or delete it)")
    user = await db.get(User, db_cred.user_id)
    if user is None or user.disabled:
        raise HTTPException(401, "Account unavailable")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=pending["challenge"],
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
            credential_public_key=db_cred.public_key,
            credential_current_sign_count=db_cred.sign_count,
            require_user_verification=False,
        )
    except Exception:
        raise HTTPException(401, "Passkey verification failed")

    db_cred.sign_count = verification.new_sign_count
    db_cred.last_used_at = utcnow()
    token = await _issue_session(db, user, request)
    await db.commit()
    return TokenOut(token=token, user=MeOut.model_validate(user))


# ---------- password login (optional, alongside passkeys) ----------


@router.post("/register/password", response_model=TokenOut)
async def register_password(
    body: RegisterPasswordIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    username = body.username.lower()
    is_first_user, invite, recover_user = await _resolve_registration(
        db, username, body.invite_code
    )
    if recover_user is not None:
        user = recover_user
        # Claiming a pre-created/recovery account: invalidate any old sessions.
        await db.execute(
            update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True)
        )
    else:
        user = User(
            username=username, display_name=body.display_name, is_admin=is_first_user
        )
        db.add(user)
        await db.flush()
        from .spaces import add_to_space, ensure_default_space

        space = await ensure_default_space(db, user.id)
        await add_to_space(
            db, space.id, user.id, "manager" if user.is_admin else "member"
        )

    existing = await db.get(PasswordCredential, user.id)
    if existing is None:
        db.add(PasswordCredential(user_id=user.id, password_hash=hash_password(body.password)))
    else:
        existing.password_hash = hash_password(body.password)
    if invite:
        invite.used_by = user.id
        invite.used_at = utcnow()
    await db.flush()
    token = await _issue_session(db, user, request)
    await db.commit()
    return TokenOut(token=token, user=MeOut.model_validate(user))


@router.post("/login/password", response_model=TokenOut)
async def login_password(
    body: LoginPasswordIn, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    user = await db.scalar(select(User).where(User.username == body.username.lower()))
    cred = await db.get(PasswordCredential, user.id) if (user and not user.disabled) else None
    # Always run exactly one verification (dummy hash when there's no credential)
    # so timing doesn't leak whether the username exists.
    ok = verify_password(cred.password_hash if cred else _DUMMY_PASSWORD_HASH, body.password)
    if not (cred and ok):
        raise HTTPException(401, "Incorrect username or password")
    token = await _issue_session(db, user, request)
    await db.commit()
    return TokenOut(token=token, user=MeOut.model_validate(user))


@router.get("/password")
async def password_status(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    cred = await db.get(PasswordCredential, user.id)
    return {"has_password": cred is not None}


@router.post("/password")
async def set_password(
    body: SetPasswordIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cred = await db.get(PasswordCredential, user.id)
    if cred is not None:
        # Changing an existing password requires the current one.
        if not body.current_password or not verify_password(
            cred.password_hash, body.current_password
        ):
            raise HTTPException(403, "Current password is incorrect")
        cred.password_hash = hash_password(body.password)
    else:
        db.add(PasswordCredential(user_id=user.id, password_hash=hash_password(body.password)))
    return {"ok": True}


@router.delete("/password", status_code=204)
async def remove_password(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    cred = await db.get(PasswordCredential, user.id)
    if cred is None:
        return
    passkeys = await db.scalar(
        select(func.count())
        .select_from(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == user.id)
    )
    if not passkeys:
        raise HTTPException(
            400, "Add a passkey before removing your password — you'd have no way to sign in."
        )
    await db.delete(cred)


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    session = await db.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )
    if session:
        session.revoked = True
    return {"ok": True}


# ---------- passkey management (signed-in) ----------


def _passkey_out(cred: WebAuthnCredential) -> PasskeyOut:
    return PasskeyOut(
        id=b64url_encode(cred.id),
        label=cred.label,
        created_at=cred.created_at,
        last_used_at=cred.last_used_at,
    )


@router.get("/passkeys", response_model=list[PasskeyOut])
async def list_passkeys(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[PasskeyOut]:
    creds = await db.scalars(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == user.id)
        .order_by(WebAuthnCredential.created_at)
    )
    return [_passkey_out(c) for c in creds]


@router.post("/passkeys/options")
async def add_passkey_options(
    body: AddPasskeyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    creds = (
        await db.scalars(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
        )
    ).all()
    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_id=user.id.bytes,
        user_name=user.username,
        user_display_name=user.display_name,
        authenticator_selection=REGISTRATION_AUTH_SELECTION,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c.id) for c in creds],
    )
    add_token = pending_passkey_adds.put(
        {"challenge": options.challenge, "user_id": user.id, "label": body.label}
    )
    return {"add_token": add_token, "options": json.loads(options_to_json(options))}


@router.post("/passkeys/verify", response_model=PasskeyOut)
async def add_passkey_verify(
    body: VerifyIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PasskeyOut:
    pending = pending_passkey_adds.pop(body.token)
    if pending is None or pending["user_id"] != user.id:
        raise HTTPException(400, "Passkey setup expired, start over")
    try:
        verification = _registration_payload(body.credential, pending["challenge"])
    except Exception:
        raise HTTPException(400, "Passkey verification failed")
    cred = WebAuthnCredential(
        id=verification.credential_id,
        user_id=user.id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        label=pending["label"],
    )
    db.add(cred)
    await db.commit()
    return _passkey_out(cred)


@router.delete("/passkeys/{credential_id}", status_code=204)
async def delete_passkey(
    credential_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    try:
        raw_id = b64url_decode(credential_id)
    except Exception:
        raise HTTPException(404, "Passkey not found")
    cred = await db.get(WebAuthnCredential, raw_id)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(404, "Passkey not found")
    count = await db.scalar(
        select(func.count())
        .select_from(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == user.id)
    )
    if count <= 1:
        raise HTTPException(400, "You can't remove your only passkey")
    await db.delete(cred)


# ---------- session management ----------


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SessionOut]:
    current_hash = hash_token(
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    sessions = await db.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked == False,  # noqa: E712
            AuthSession.expires_at > utcnow(),
        )
        .order_by(AuthSession.last_seen_at.desc())
    )
    return [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            user_agent=s.user_agent,
            current=s.token_hash == current_hash,
        )
        for s in sessions
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    session = await db.get(AuthSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    session.revoked = True
