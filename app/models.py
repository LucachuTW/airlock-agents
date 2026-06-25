import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        str: Text,
        dict: JSONB,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=text("now()"))


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    role: Mapped[str]  # admin | analyst | viewer (CHECK in migration)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _now()


class Tool(Base):
    __tablename__ = "tools"
    name: Mapped[str] = mapped_column(primary_key=True)
    mcp_server: Mapped[str]
    description: Mapped[str]
    risk: Mapped[str]  # read | write | critical
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class RoleToolGrant(Base):
    __tablename__ = "role_tool_grants"
    role: Mapped[str] = mapped_column(primary_key=True)
    tool_name: Mapped[str] = mapped_column(ForeignKey("tools.name"), primary_key=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    agent: Mapped[str | None]   # what to rebuild on resume: "auto" (supervisor) or a direct agent
    route: Mapped[str | None]   # subagent that actually handled it (metrics / routing evals)
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    input: Mapped[str]
    output: Mapped[str | None]
    trace_id: Mapped[str | None]
    model: Mapped[str | None]
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None]


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    tool_name: Mapped[str] = mapped_column(ForeignKey("tools.name"))
    payload: Mapped[dict]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None]
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None]
    action: Mapped[str]
    resource: Mapped[str | None]
    detail: Mapped[dict | None]
    trace_id: Mapped[str | None]
    created_at: Mapped[datetime] = _now()


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str]
    source_uri: Mapped[str]
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = _now()


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    ord: Mapped[int] = mapped_column(Integer)
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(768))


class Ticket(Base):
    """Demo target system for the `actions` agent — stands in for Jira/ServiceNow."""

    __tablename__ = "tickets"
    id: Mapped[uuid.UUID] = _uuid_pk()
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True)
    title: Mapped[str]
    body: Mapped[str]
    priority: Mapped[str]
    created_at: Mapped[datetime] = _now()


class EvalCase(Base):
    __tablename__ = "eval_cases"
    id: Mapped[uuid.UUID] = _uuid_pk()
    suite: Mapped[str]
    input: Mapped[str]
    expected: Mapped[dict]
    case_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    suite: Mapped[str]
    kind: Mapped[str]  # offline | online
    git_sha: Mapped[str | None]
    model: Mapped[str | None]
    created_at: Mapped[datetime] = _now()


class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[uuid.UUID] = _uuid_pk()
    eval_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_runs.id"))
    case_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"))
    scores: Mapped[dict]
    passed: Mapped[bool] = mapped_column(Boolean)
    tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int] = mapped_column(SmallInteger)  # -1 | 1
    comment: Mapped[str | None]
    created_at: Mapped[datetime] = _now()
