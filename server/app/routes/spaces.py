import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import require_space_manager, require_space_member
from .. import events, storage
from ..db import DB, after_commit, commit
from ..deps import get_admin_user, get_current_user
from ..membership import (
    add_to_space,
    ensure_channel_owner,
    ensure_space_manager,
    space_member_ids,
)
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
from ..vocab import SpaceRole

router = APIRouter(prefix="/api/v1/spaces", tags=["spaces"])


def space_out(space: Space, role: str | None) -> SpaceOut:
    return SpaceOut(
        id=space.id, name=space.name, is_default=space.is_default, my_role=role
    )


@router.get("", response_model=list[SpaceOut])
async def my_spaces(
    db: AsyncSession = DB, user: User = Depends(get_current_user)
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
    db: AsyncSession = DB,
    admin: User = Depends(get_admin_user),
) -> None:
    """Set the global top-to-bottom order of spaces (admin only). `order` is the
    space ids in the desired order; each gets position = its index."""
    # One statement instead of one UPDATE per space: CASE maps each id to its index.
    positions = {sid: i for i, sid in enumerate(body.order)}
    if positions:
        await db.execute(
            update(Space)
            .where(Space.id.in_(list(positions)))
            .values(position=case(positions, value=Space.id))
        )


@router.post("", response_model=SpaceOut, status_code=201)
async def create_space(
    body: SpaceIn,
    db: AsyncSession = DB,
    admin: User = Depends(get_admin_user),
) -> SpaceOut:
    max_pos = await db.scalar(select(func.max(Space.position)))
    space = Space(
        name=body.name.strip(), created_by=admin.id, position=(max_pos or 0) + 1
    )
    db.add(space)
    await db.flush()
    db.add(SpaceMember(space_id=space.id, user_id=admin.id, role=SpaceRole.MANAGER))
    return space_out(space, SpaceRole.MANAGER)


@router.patch("/{space_id}", response_model=SpaceOut)
async def update_space(
    space_id: uuid.UUID,
    body: SpaceIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> SpaceOut:
    space = await require_space_manager(db, space_id, user)
    space.name = body.name.strip()
    member = await db.get(SpaceMember, (space_id, user.id))
    await db.flush()
    events.publish(
        db, await space_member_ids(db, space_id), {"type": "channels.changed"}
    )
    return space_out(space, member.role if member else None)


@router.delete("/{space_id}", status_code=204)
async def delete_space(
    space_id: uuid.UUID,
    db: AsyncSession = DB,
    admin: User = Depends(get_admin_user),
) -> None:
    space = await db.get(Space, space_id)
    if space is None:
        raise HTTPException(404, "Space not found")
    if space.is_default:
        raise HTTPException(400, "The default space can't be deleted")
    member_ids = await space_member_ids(db, space_id)
    file_ids = (
        await db.scalars(
            select(File.id)
            .join(Channel, Channel.id == File.channel_id)
            .where(Channel.space_id == space_id)
        )
    ).all()
    await db.delete(space)  # cascades channels → messages → members
    events.publish(db, member_ids, {"type": "channels.changed"})
    after_commit(db, storage.unlink_blobs, file_ids)
    await commit(db)


@router.get("/{space_id}/members", response_model=list[SpaceMemberOut])
async def list_space_members(
    space_id: uuid.UUID,
    db: AsyncSession = DB,
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
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> SpaceMemberOut:
    await require_space_manager(db, space_id, user)
    target = await db.get(User, body.user_id)
    if target is None or target.disabled:
        raise HTTPException(404, "User not found")
    await add_to_space(db, space_id, target.id, body.role)
    await db.flush()
    events.publish(db, [target.id], {"type": "channels.changed"})
    return SpaceMemberOut(**UserOut.model_validate(target).model_dump(), role=body.role)


@router.patch("/{space_id}/members/{user_id}", response_model=SpaceMemberOut)
async def set_member_role(
    space_id: uuid.UUID,
    user_id: uuid.UUID,
    body: SpaceMemberRoleIn,
    db: AsyncSession = DB,
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
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    space = await require_space_manager(db, space_id, user)
    if space.is_default:
        raise HTTPException(400, "Members can't be removed from the default space")
    member = await db.get(SpaceMember, (space_id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    was_manager = member.role == SpaceRole.MANAGER
    await db.delete(member)
    # Drop their membership of every channel in this space in one statement.
    await db.execute(
        delete(ChannelMember).where(
            ChannelMember.user_id == user_id,
            ChannelMember.channel_id.in_(
                select(Channel.id).where(Channel.space_id == space_id)
            ),
        )
    )
    await db.flush()
    if was_manager:
        await ensure_space_manager(db, space_id)
    # Channels they owned in this space need an owner too.
    owned = await db.scalars(
        select(Channel.id).where(
            Channel.space_id == space_id, Channel.is_dm == False  # noqa: E712
        )
    )
    for cid in owned.all():
        await ensure_channel_owner(db, cid)
    events.publish(db, [user_id], {"type": "channels.changed"})
