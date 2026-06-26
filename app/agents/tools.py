"""Tool registry -> runtime binding.

Loads the tools granted to a role from the DB, connects to the owning MCP servers
(stdio spawn in dev; streamable-http in K8s) and wraps every tool so each call is
audit-logged with the run and actor.
"""

import os
import sys
import uuid
from copy import deepcopy
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import telemetry
from app.config import settings
from app.db import SessionLocal
from app.models import RoleToolGrant, Tool
from app.services.audit import audit

ROOT = Path(__file__).resolve().parent.parent.parent
SYNC_DB_URL = settings.database_url.replace("+asyncpg", "")


def _server_spec(server: str) -> dict:
    url = os.environ.get(f"MCP_HTTP_{server.upper()}")  # set in K8s -> http transport
    if url:
        return {"transport": "streamable_http", "url": url}
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", f"mcp_servers.{server}.server"],
        "cwd": str(ROOT),
        "env": {
            "PATH": os.environ.get("PATH", ""),
            "MCP_DATABASE_URL": SYNC_DB_URL,
            "MCP_DB_URL": os.environ.get(
                "MCP_DB_URL", "postgresql://mcp_readonly:mcp_readonly@localhost:5432/agentic"
            ),
            "OLLAMA_BASE_URL": settings.ollama_base_url,
        },
    }


async def granted_tools(session: AsyncSession, role: str) -> list[Tool]:
    q = (
        select(Tool)
        .join(RoleToolGrant, RoleToolGrant.tool_name == Tool.name)
        .where(RoleToolGrant.role == role, Tool.enabled)
    )
    return list((await session.execute(q)).scalars().all())


async def build_tools(
    granted: list[Tool], *, run_id: uuid.UUID, actor_id: uuid.UUID
) -> list[BaseTool]:
    """MCP tools for the granted registry entries, wrapped with audit logging."""
    if not granted:
        return []
    servers = sorted({t.mcp_server for t in granted})
    client = MultiServerMCPClient({s: _server_spec(s) for s in servers})  # type: ignore[misc, arg-type]
    mcp_tools = await client.get_tools()
    allowed = {t.name for t in granted}
    return [_audited(t, run_id=run_id, actor_id=actor_id) for t in mcp_tools if t.name in allowed]


def _audited(tool: BaseTool, *, run_id: uuid.UUID, actor_id: uuid.UUID) -> BaseTool:
    async def call(**kwargs):
        async with SessionLocal() as s:
            await audit(s, "tool.call", actor_id=actor_id, run_id=run_id,
                        resource=tool.name, detail={"args": kwargs})
            await s.commit()
        try:
            result = await tool.ainvoke(kwargs)
        except Exception as exc:
            telemetry.TOOL_CALLS.labels(tool=tool.name, status="error").inc()
            async with SessionLocal() as s:
                await audit(s, "tool.error", actor_id=actor_id, run_id=run_id,
                            resource=tool.name, detail={"error": str(exc)[:500]})
                await s.commit()
            raise
        telemetry.TOOL_CALLS.labels(tool=tool.name, status="ok").inc()
        async with SessionLocal() as s:
            await audit(s, "tool.result", actor_id=actor_id, run_id=run_id,
                        resource=tool.name, detail={"result": str(result)[:1000]})
            await s.commit()
        return result

    return StructuredTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=tool.args_schema,  # type: ignore[arg-type]  # MCP adapters use dict schemas
        coroutine=call,
    )


def hitl_gate(tool: BaseTool, run_id: uuid.UUID) -> BaseTool:
    """Wrap a critical tool with a human-approval interrupt.

    The agent-facing schema hides `approval_id`: the agent proposes a payload, the graph
    pauses, and on resume the tool executes the payload the human approved — never a
    regenerated one. The MCP server re-verifies payload equality as the final guard.

    ponytail: one critical action per run — small local models sometimes retry an already
    successful call with a regenerated payload; the audit trail is the execution marker.
    """
    schema = deepcopy(tool.args_schema)
    required: list[str] = []
    if isinstance(schema, dict):
        schema.get("properties", {}).pop("approval_id", None)
        if "approval_id" in schema.get("required", []):
            schema["required"].remove("approval_id")
        required = list(schema.get("required", []))

    async def call(**kwargs):
        from sqlalchemy import select

        from app.models import AuditLog

        # Never ask a human to approve an unexecutable payload.
        missing = [f for f in required if kwargs.get(f) in (None, "")]
        if missing:
            return (f"Invalid draft: missing required field(s) {missing}. "
                    f"Call {tool.name} again with ALL fields filled in.")

        async with SessionLocal() as s:
            executed = (
                await s.execute(
                    select(AuditLog.id).where(
                        AuditLog.run_id == run_id,
                        AuditLog.action == "tool.result",
                        AuditLog.resource == tool.name,
                    ).limit(1)
                )
            ).first()
        if executed:
            return (f"{tool.name} was ALREADY executed once in this request. Do not call it "
                    "again — report the previous result to the user.")
        decision = interrupt({"tool": tool.name, "payload": kwargs})
        if decision["status"] == "approved":
            return await tool.ainvoke({**decision["payload"], "approval_id": decision["approval_id"]})
        note = decision.get("note") or "no reason given"
        return f"REJECTED by the human approver (note: {note}). Do not retry; inform the user."

    return StructuredTool(
        name=tool.name,
        description=(tool.description or "") + " Execution requires human approval.",
        args_schema=schema,  # type: ignore[arg-type]  # MCP adapters use dict schemas
        coroutine=call,
    )
