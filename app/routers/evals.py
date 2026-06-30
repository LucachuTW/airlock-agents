import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_role
from app.db import get_session
from app.models import EvalResult, EvalRun

router = APIRouter(prefix="/evals", tags=["evals"],
                   dependencies=[Depends(require_role("admin"))])


@router.get("/runs")
async def list_eval_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    suite: str | None = None,
    limit: int = 50,
):
    q = select(EvalRun).order_by(EvalRun.created_at.desc()).limit(min(limit, 200))
    if suite:
        q = q.where(EvalRun.suite == suite)
    eval_runs = (await session.execute(q)).scalars().all()
    out = []
    for er in eval_runs:
        results = (
            (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == er.id)))
            .scalars().all()
        )
        keys = sorted({k for r in results for k, v in r.scores.items() if isinstance(v, (int, float))})
        aggregate = {
            k: round(sum(r.scores.get(k, 0.0) for r in results
                         if isinstance(r.scores.get(k), (int, float))) / len(results), 3)
            for k in keys
        } if results else {}
        out.append({
            "eval_run_id": str(er.id), "suite": er.suite, "kind": er.kind,
            "git_sha": er.git_sha, "model": er.model, "created_at": er.created_at.isoformat(),
            "cases": len(results), "passed": sum(r.passed for r in results),
            "aggregate": aggregate,
        })
    return out


@router.get("/runs/{eval_run_id}")
async def get_eval_run(
    eval_run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    er = await session.get(EvalRun, eval_run_id)
    if er is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Eval run not found")
    results = (
        (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == er.id)))
        .scalars().all()
    )
    return {
        "eval_run_id": str(er.id), "suite": er.suite, "kind": er.kind,
        "git_sha": er.git_sha, "model": er.model, "created_at": er.created_at.isoformat(),
        "results": [
            {"case_id": str(r.case_id) if r.case_id else None,
             "run_id": str(r.run_id) if r.run_id else None,
             "scores": r.scores, "passed": r.passed,
             "tokens": r.tokens, "latency_ms": r.latency_ms}
            for r in results
        ],
    }
