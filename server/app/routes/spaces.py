import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_admin_user, get_current_user
from ..models import (
    Channel,
    ChannelMember,
    File,
    Space,
    SpaceMember,
    User,
)
from ..schemas import (
    SpaceIn,
    SpaceMemberIn,
    SpaceMemberOut,
    SpaceMemberRoleIn,
    SpaceOrderIn,
    SpaceOut,
    UserOut,
)
from ..ws import manager

router = APIRouter(prefix="/api/v1/spaces", tags=["spaces"])


async def ensure_default_space(db: AsyncSession, creator_id: uuid.UUID) -> Space:
    """The 'everyone' space new users auto-join. Created on demand."""
    space = await db.scalar(select(Space).where(Space.is_default == True))  # noqa: E712
    if space is None:
        space = Space(name="Main", is_default=True, created_by=creator_id)
        db.add(space)
        await db.flush()
    return space


async def add_to_space(
    db: AsyncSession, space_id: uuid.UUID, user_id: uuid.UUID, role: str = "member"
) -> None:
    """Add a user to a space and auto-join its public channels."""
    if await db.get(SpaceMember, (space_id, user_id)) is None:
        db.add(SpaceMember(space_id=space_id, user_id=user_id, role=role))
    public = await db.scalars(
        select(Channel.id).where(
            Channel.space_id == space_id,
            Channel.is_private == False,  # noqa: E712
            Channel.is_dm == False,  # noqa: E712
        )
    )
    for channel_id in public.all():
        if await db.get(ChannelMember, (channel_id, user_id)) is None:
            db.add(ChannelMember(channel_id=channel_id, user_id=user_id))


async def require_space_member(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> Space:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if user.is_admin:
        return space
    if await db.get(SpaceMember, (space_id, user.id)) is None:
        raise HTTPException(404, "Space not found")  # don't leak existence
    return space


async def require_space_manager(
    db: AsyncSession, space_id: uuid.UUID, user: User
) -> Space:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if user.is_admin:
        return space
    member = await db.get(SpaceMember, (space_id, user.id))
    if member is None:
        raise HTTPException(404, "Space not found")
    if member.role != "manager":
        raise HTTPException(403, "Only a space manager can do that")
    return space


def space_out(space: Space, role: str | None) -> SpaceOut:
    return SpaceOut(
        id=space.id, name=space.name, is_default=space.is_default, my_role=role
    )


@router.get("", response_model=list[SpaceOut])
async def my_spaces(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[SpaceOut]:
    rows = (
        await db.execute(
            select(Space, SpaceMember.role)
            .join(SpaceMember, SpaceMember.space_id == Space.id)
            .where(SpaceMember.user_id == user.id)
            .order_by(Space.position, Space.name)
        )
    ).all()
    spaces = [space_out(s, role) for s, role in rows]
    if user.is_admin:
        # Admins see every space even if not a member, so they can manage it.
        seen = {s.id for s in spaces}
        q = select(Space).order_by(Space.is_default.desc(), Space.name)
        if seen:
            q = q.where(Space.id.not_in(seen))
        for s in (await db.scalars(q)).all():
            spaces.append(space_out(s, None))
    return spaces


@router.put("/order", status_code=204)
async def reorder_spaces(
    body: SpaceOrderIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    """Set the global top-to-bottom order of spaces (admin only). `order` is the
    space ids in the desired order; each gets position = its index."""
    for i, sid in enumerate(body.order):
        await db.execute(update(Space).where(Space.id == sid).values(position=i))


@router.post("", response_model=SpaceOut, status_code=201)
async def create_space(
    body: SpaceIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> SpaceOut:
    max_pos = await db.scalar(select(func.max(Space.position)))
    space = Space(
        name=body.name.strip(), created_by=admin.id, position=(max_pos or 0) + 1
    )
    db.add(space)
    await db.flush()
    db.add(SpaceMember(space_id=space.id, user_id=admin.id, role="manager"))
    return space_out(space, "manager")


@router.patch("/{space_id}", response_model=SpaceOut)
async def update_space(
    space_id: uuid.UUID,
    body: SpaceIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SpaceOut:
    space = await require_space_manager(db, space_id, user)
    space.name = body.name.strip()
    member = await db.get(SpaceMember, (space_id, user.id))
    await db.flush()
    member_ids = (
        await db.scalars(select(SpaceMember.user_id).where(SpaceMember.space_id == space_id))
    ).all()
    await manager.send_to_users(member_ids, {"type": "channels.changed"})
    return space_out(space, member.role if member else None)


@router.delete("/{space_id}", status_code=204)
async def delete_space(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> None:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if space.is_default:
        raise HTTPException(400, "The default space can't be deleted")
    member_ids = (
        await db.scalars(select(SpaceMember.user_id).where(SpaceMember.space_id == space_id))
    ).all()
    file_ids = (
        await db.scalars(
            select(File.id)
            .join(Channel, Channel.id == File.channel_id)
            .where(Channel.space_id == space_id)
        )
    ).all()
    await db.delete(space)  # cascades channels → messages → members
    await db.commit()
    for file_id in file_ids:
        (Path(settings.upload_dir) / str(file_id)).unlink(missing_ok=True)
    await manager.send_to_users(member_ids, {"type": "channels.changed"})


@router.get("/{space_id}/members", response_model=list[SpaceMemberOut])
async def list_space_members(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SpaceMemberOut]:
    await require_space_member(db, space_id, user)
    rows = (
        await db.execute(
            select(User, SpaceMember.role)
            .join(SpaceMember, SpaceMember.user_id == User.id)
            .where(SpaceMember.space_id == space_id)
            .order_by(SpaceMember.role, User.username)
        )
    ).all()
    return [
        SpaceMemberOut(**UserOut.model_validate(u).model_dump(), role=role)
        for u, role in rows
    ]


@router.post("/{space_id}/members", response_model=SpaceMemberOut, status_code=201)
async def add_space_member(
    space_id: uuid.UUID,
    body: SpaceMemberIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SpaceMemberOut:
    await require_space_manager(db, space_id, user)
    target = await db.get(User, body.user_id)
    if target is None or target.disabled:
        raise HTTPException(404, "User not found")
    await add_to_space(db, space_id, target.id, body.role)
    await db.flush()
    await manager.send_to_users([target.id], {"type": "channels.changed"})
    return SpaceMemberOut(**UserOut.model_validate(target).model_dump(), role=body.role)


@router.patch("/{space_id}/members/{user_id}", response_model=SpaceMemberOut)
async def set_member_role(
    space_id: uuid.UUID,
    user_id: uuid.UUID,
    body: SpaceMemberRoleIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SpaceMemberOut:
    await require_space_manager(db, space_id, user)
    member = await db.get(SpaceMember, (space_id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    member.role = body.role
    target = await db.get(User, user_id)
    return SpaceMemberOut(**UserOut.model_validate(target).model_dump(), role=body.role)


@router.delete("/{space_id}/members/{user_id}", status_code=204)
async def remove_space_member(
    space_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    space = await require_space_manager(db, space_id, user)
    if space.is_default:
        raise HTTPException(400, "Members can't be removed from the default space")
    member = await db.get(SpaceMember, (space_id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    await db.delete(member)
    # Drop their membership of every channel in this space.
    channel_ids = (
        await db.scalars(select(Channel.id).where(Channel.space_id == space_id))
    ).all()
    for channel_id in channel_ids:
        cm = await db.get(ChannelMember, (channel_id, user_id))
        if cm is not None:
            await db.delete(cm)
    await db.flush()
    await manager.send_to_users([user_id], {"type": "channels.changed"})
