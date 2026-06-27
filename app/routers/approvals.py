import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import telemetry
from app.auth import require_role
from app.db import get_session
from app.models import Approval, Run, User
from app.services.audit import audit
from app.services.runs import resume_run

router = APIRouter(prefix="/approvals", tags=["approvals"])

Approver = Annotated[User, Depends(require_role("admin", "analyst"))]


def _out(a: Approval) -> dict:
    return {
        "approval_id": str(a.id), "run_id": str(a.run_id), "tool_name": a.tool_name,
        "payload": a.payload, "status": a.status,
        "decided_by": str(a.decided_by) if a.decided_by else None,
        "decision_note": a.decision_note,
        "expires_at": a.expires_at.isoformat(), "created_at": a.created_at.isoformat(),
    }


@router.get("")
async def list_approvals(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Approver,
    approval_status: str = "pending",
    limit: int = 100,
):
    q = (
        select(Approval)
        .where(Approval.status == approval_status)
        .order_by(Approval.created_at.desc())
        .limit(min(limit, 500))
    )
    return [_out(a) for a in (await session.execute(q)).scalars().all()]


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None


@router.post("/{approval_id}")
async def decide(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    approver: Approver,
):
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already {approval.status}")
    if approval.expires_at < datetime.now(UTC):
        approval.status = "expired"
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "Approval expired")

    run = await session.get(Run, approval.run_id)
    assert run is not None  # approvals.run_id is a FK
    if run.user_id == approver.id and approver.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot approve your own run")

    approval.status = body.decision
    approval.decided_by = approver.id
    approval.decision_note = body.note
    approval.decided_at = datetime.now(UTC)
    telemetry.APPROVALS_TOTAL.labels(decision=body.decision).inc()
    telemetry.APPROVAL_WAIT.observe((approval.decided_at - approval.created_at).total_seconds())
    await audit(session, "approval.decided", actor_id=approver.id, run_id=approval.run_id,
                resource=approval.tool_name,
                detail={"approval_id": str(approval.id), "decision": body.decision, "note": body.note})
    await session.commit()

    decision = {
        "status": body.decision,
        "payload": approval.payload,       # the exact payload the human saw
        "approval_id": str(approval.id),
        "note": body.note,
    }
    asyncio.create_task(
        resume_run(approval.run_id, decision,
                   request.app.state.redis, request.app.state.checkpointer)
    )
    return {"approval_id": str(approval.id), "status": body.decision, "run_id": str(approval.run_id)}
