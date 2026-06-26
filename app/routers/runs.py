import asyncio
import json
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agents.tools import granted_tools
from app.auth import get_current_user
from app.db import get_session
from app.models import Feedback, Run, User
from app.services.audit import audit
from app.services.runs import AGENTS, channel_for, execute_run

router = APIRouter(prefix="/runs", tags=["runs"])

FINISHED = ("completed", "failed", "rejected")


def _run_out(run: Run) -> dict:
    return {
        "run_id": str(run.id), "status": run.status, "agent": run.agent, "route": run.route,
        "input": run.input, "output": run.output, "model": run.model,
        "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
        "latency_ms": run.latency_ms, "trace_id": run.trace_id,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


async def _owned_run(run_id: uuid.UUID, session: AsyncSession, user: User) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if run.user_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")
    return run


class StartRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # "auto" = supervisor routes to a specialist; direct names are used by the eval suite
    agent: Literal["auto", "docs_qa", "analyst", "actions"] = "auto"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    body: StartRunRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    granted = await granted_tools(session, user.role)
    needed = None if body.agent == "auto" else AGENTS[body.agent][1]
    if not any(needed is None or t.mcp_server in needed for t in granted):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Your role has no tools for agent '{body.agent}'")
    run = Run(user_id=user.id, input=body.message, status="running", agent=body.agent)
    session.add(run)
    await session.flush()
    await audit(session, "run.started", actor_id=user.id, run_id=run.id,
                detail={"agent": body.agent})
    await session.commit()
    asyncio.create_task(
        execute_run(run.id, user.id, user.role, body.message, body.agent,
                    request.app.state.redis, request.app.state.checkpointer)
    )
    return {"run_id": str(run.id), "stream_url": f"/api/v1/runs/{run.id}/events"}


@router.get("/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    run = await _owned_run(run_id, session, user)
    redis = request.app.state.redis

    async def gen():
        if run.status in FINISHED:
            yield {"event": "message", "data": json.dumps({"type": "done", **_run_out(run)})}
            return
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_for(run_id))
        try:
            # re-check after subscribing: the run may have finished in between
            async with request.app.state.sessionmaker() as s:
                current = await s.get(Run, run_id)
            if current.status in FINISHED:
                yield {"event": "message", "data": json.dumps({"type": "done", **_run_out(current)})}
                return
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if msg is None:
                    yield {"event": "ping", "data": ""}
                    continue
                data = msg["data"].decode()
                yield {"event": "message", "data": data}
                if json.loads(data).get("type") in ("done", "error"):
                    return
        finally:
            await pubsub.unsubscribe(channel_for(run_id))
            await pubsub.aclose()

    return EventSourceResponse(gen())


@router.get("")
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    run_status: str | None = None,
    limit: int = 50,
):
    q = select(Run).order_by(Run.created_at.desc()).limit(min(limit, 500))
    if user.role != "admin":
        q = q.where(Run.user_id == user.id)
    if run_status:
        q = q.where(Run.status == run_status)
    return [_run_out(r) for r in (await session.execute(q)).scalars().all()]


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    return _run_out(await _owned_run(run_id, session, user))


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = None


@router.post("/{run_id}/feedback", status_code=status.HTTP_201_CREATED)
async def leave_feedback(
    run_id: uuid.UUID,
    body: FeedbackRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    await _owned_run(run_id, session, user)
    stmt = (
        pg_insert(Feedback)
        .values(run_id=run_id, user_id=user.id, rating=body.rating, comment=body.comment)
        .on_conflict_do_update(
            index_elements=["run_id", "user_id"],
            set_={"rating": body.rating, "comment": body.comment},
        )
    )
    await session.execute(stmt)
    await session.commit()
    return {"run_id": str(run_id), "rating": body.rating}
