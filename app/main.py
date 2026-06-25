import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import select, text

from app import llm, telemetry
from app.config import settings
from app.db import SessionLocal, engine
from app.models import Approval, Run
from app.routers import admin, approvals, auth, evals, runs
from app.services.audit import audit

log = logging.getLogger("agentic")


async def expire_approvals_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            async with SessionLocal() as session:
                stale = (
                    await session.execute(
                        select(Approval).where(
                            Approval.status == "pending",
                            Approval.expires_at < datetime.now(UTC),
                        )
                    )
                ).scalars().all()
                for approval in stale:
                    approval.status = "expired"
                    telemetry.APPROVALS_TOTAL.labels(decision="expired").inc()
                    run = await session.get(Run, approval.run_id)
                    assert run is not None  # approvals.run_id is a FK
                    run.status = "failed"
                    run.output = "Approval request expired before a human decided."
                    run.finished_at = datetime.now(UTC)
                    await audit(session, "approval.expired", run_id=approval.run_id,
                                resource=approval.tool_name,
                                detail={"approval_id": str(approval.id)})
                await session.commit()
        except Exception:
            log.exception("approval expiry sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    models = llm.resolve()
    log.info("model tier=%s chat=%s embeddings=%s", models["tier"], models["chat"], models["embeddings"])
    app.state.redis = aioredis.from_url(settings.redis_url)
    app.state.sessionmaker = SessionLocal

    # LangGraph checkpointer (psycopg pool): durable runs + HITL resume
    psy_url = settings.database_url.replace("+asyncpg", "")
    pool = AsyncConnectionPool(
        psy_url, open=False, min_size=1, max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]  # kwargs row_factory=dict_row
    await checkpointer.setup()
    app.state.checkpointer = checkpointer

    expiry_task = asyncio.create_task(expire_approvals_loop())
    yield
    expiry_task.cancel()
    await pool.close()
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="Agentic Enterprise Platform", lifespan=lifespan)
telemetry.setup_otel(app, engine)
app.mount("/metrics", telemetry.metrics_app)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(admin.tools_router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(approvals.router, prefix="/api/v1")
app.include_router(evals.router, prefix="/api/v1")


@app.get("/health")
async def health():
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    await app.state.redis.ping()
    return {"status": "ok", "models": llm.resolve()}
