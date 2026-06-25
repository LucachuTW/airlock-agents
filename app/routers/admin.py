import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ROLES, get_current_user, hash_password, require_role
from app.db import get_session
from app.models import AuditLog, RoleToolGrant, Tool, User
from app.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])
tools_router = APIRouter(tags=["tools"])


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    role: Literal["admin", "analyst", "viewer"]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[User, Depends(require_role("admin"))],
):
    if (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(email=body.email, password_hash=hash_password(body.password), role=body.role)
    session.add(user)
    await session.flush()
    await audit(session, "admin.user_created", actor_id=actor.id, resource=str(user.id),
                detail={"email": body.email, "role": body.role})
    await session.commit()
    return {"id": str(user.id), "email": user.email, "role": user.role}


class GrantRequest(BaseModel):
    role: Literal["admin", "analyst", "viewer"]
    tool_name: str
    granted: bool


@router.put("/grants")
async def set_grant(
    body: GrantRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[User, Depends(require_role("admin"))],
):
    if await session.get(Tool, body.tool_name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tool: {body.tool_name}")
    if body.granted:
        if await session.get(RoleToolGrant, (body.role, body.tool_name)) is None:
            session.add(RoleToolGrant(role=body.role, tool_name=body.tool_name))
    else:
        await session.execute(
            delete(RoleToolGrant)
            .where(RoleToolGrant.role == body.role, RoleToolGrant.tool_name == body.tool_name)
        )
    await audit(session, "admin.grant", actor_id=actor.id, resource=body.tool_name,
                detail={"role": body.role, "granted": body.granted})
    await session.commit()
    return {"role": body.role, "tool_name": body.tool_name, "granted": body.granted}


@router.get("/audit")
async def list_audit(
    session: Annotated[AsyncSession, Depends(get_session)],
    run_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = 100,
):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    if run_id:
        q = q.where(AuditLog.run_id == run_id)
    if action:
        q = q.where(AuditLog.action == action)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": r.id, "actor_id": str(r.actor_id) if r.actor_id else None,
            "run_id": str(r.run_id) if r.run_id else None, "action": r.action,
            "resource": r.resource, "detail": r.detail, "trace_id": r.trace_id,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@tools_router.get("/tools")
async def my_tools(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Tools visible to the calling user's role."""
    q = (
        select(Tool)
        .join(RoleToolGrant, RoleToolGrant.tool_name == Tool.name)
        .where(RoleToolGrant.role == user.role, Tool.enabled)
    )
    tools = (await session.execute(q)).scalars().all()
    return [
        {"name": t.name, "description": t.description, "mcp_server": t.mcp_server, "risk": t.risk}
        for t in tools
    ]


assert set(ROLES) == {"admin", "analyst", "viewer"}  # keep Literal types above in sync
