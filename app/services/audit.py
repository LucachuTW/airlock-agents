import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def audit(
    session: AsyncSession,
    action: str,
    *,
    actor_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    resource: str | None = None,
    detail: dict | None = None,
    trace_id: str | None = None,
) -> None:
    """Append an audit row. The app DB role has no UPDATE/DELETE on audit_log (see migration)."""
    if trace_id is None:
        trace_id = current_trace_id()
    session.add(
        AuditLog(
            actor_id=actor_id, run_id=run_id, action=action,
            resource=resource, detail=detail, trace_id=trace_id,
        )
    )
    await session.flush()


def current_trace_id() -> str | None:
    from opentelemetry import trace

    span = trace.get_current_span()
    ctx = span.get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None
