"""Run execution: role-scoped agents, Redis pub/sub streaming, HITL interrupts.

Default mode is "auto": a supervisor graph routes the request to one of the specialist
subagents (built only with the tools granted to the caller's role). A run that hits a
`critical` tool pauses via LangGraph interrupt: state is checkpointed in Postgres, an
approval row is created, and `resume_run` later continues from the exact same point —
surviving API restarts in between.
"""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from langgraph.types import Command
from opentelemetry import trace

from app import llm, telemetry
from app.agents import actions, analyst, docs_qa, graph
from app.agents import tools as toolbind
from app.config import settings
from app.db import SessionLocal
from app.models import Approval, Run, User
from app.services.audit import audit, current_trace_id

AGENTS = {
    "docs_qa": (docs_qa, {"docs"}),
    "analyst": (analyst, {"db"}),
    "actions": (actions, {"tickets"}),
}

tracer = trace.get_tracer("agentic")


def channel_for(run_id: uuid.UUID) -> str:
    return f"run:{run_id}"


async def _subagent(agent_name: str, granted, *, run_id, actor_id, checkpointer=None):
    module, servers = AGENTS[agent_name]
    rows = [t for t in granted if t.mcp_server in servers]
    if not rows:
        return None
    tools = await toolbind.build_tools(rows, run_id=run_id, actor_id=actor_id)
    risk = {t.name: t.risk for t in rows}
    tools = [toolbind.hitl_gate(t, run_id) if risk.get(t.name) == "critical" else t for t in tools]
    return module.build(tools, checkpointer, name=agent_name)


async def build_agent(agent_name: str, granted, *, run_id, actor_id, checkpointer):
    if agent_name == "auto":
        subagents = []
        for name in AGENTS:
            sub = await _subagent(name, granted, run_id=run_id, actor_id=actor_id)
            if sub is not None:
                subagents.append(sub)
        if not subagents:
            raise PermissionError("role has no tools granted")
        return graph.build(subagents, checkpointer)
    agent = await _subagent(agent_name, granted, run_id=run_id, actor_id=actor_id,
                            checkpointer=checkpointer)
    if agent is None:
        raise PermissionError(f"role has no tools for agent '{agent_name}'")
    return agent


async def execute_run(run_id, user_id, role, message, agent_name, redis, checkpointer) -> None:
    await _drive(run_id, user_id, role, agent_name,
                 {"messages": [("user", message)]}, redis, checkpointer)


async def resume_run(run_id: uuid.UUID, decision: dict, redis, checkpointer) -> None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.agent is not None
        user = await session.get(User, run.user_id)
        assert user is not None
    override = "rejected" if decision["status"] == "rejected" else None
    await _drive(run_id, run.user_id, user.role, run.agent,
                 Command(resume=decision), redis, checkpointer, status_override=override)


async def _drive(run_id, user_id, role, agent_name, payload, redis, checkpointer,
                 status_override: str | None = None) -> None:
    async def publish(event: dict) -> None:
        await redis.publish(channel_for(run_id), json.dumps(event, ensure_ascii=False))

    t0 = time.monotonic()
    tokens_in = tokens_out = 0
    interrupt_value: dict | None = None
    routed: str | None = None
    final_text = ""
    status = "completed"

    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("run.id", str(run_id))
        span.set_attribute("run.agent", agent_name)
        try:
            config: dict = {"configurable": {"thread_id": str(run_id)}}
            if handler := telemetry.langfuse_handler():
                config["callbacks"] = [handler]
                config["metadata"] = {"langfuse_session_id": str(run_id)}
            async with SessionLocal() as session:
                granted = await toolbind.granted_tools(session, role)
            agent = await build_agent(agent_name, granted,
                                      run_id=run_id, actor_id=user_id, checkpointer=checkpointer)
            async for event in agent.astream(payload, config, stream_mode="updates"):
                if "__interrupt__" in event:
                    interrupt_value = event["__interrupt__"][0].value
                    continue
                for node, update in event.items():
                    if node in AGENTS:
                        routed = node
                    for msg in (update or {}).get("messages", []):
                        usage = getattr(msg, "usage_metadata", None)
                        if usage:
                            tokens_in += usage.get("input_tokens", 0)
                            tokens_out += usage.get("output_tokens", 0)
                        if msg.type == "ai":
                            if msg.content:
                                final_text = msg.content
                            for tc in getattr(msg, "tool_calls", []):
                                if tc["name"].startswith("transfer_to_"):
                                    routed = tc["name"].removeprefix("transfer_to_")
                                    await publish({"type": "route", "agent": routed})
                                else:
                                    await publish({"type": "tool_call", "tool": tc["name"],
                                                   "args": tc["args"]})
                        elif msg.type == "tool" and not str(msg.name).startswith("transfer_"):
                            await publish({"type": "tool_result", "tool": msg.name})
            output = final_text
        except Exception as exc:  # surface the failure in the run record, never crash the API
            status, output = "failed", f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if interrupt_value is not None and status != "failed":
        if routed is None:  # interrupted before the subagent node completed: derive from the tool
            server = next((t.mcp_server for t in granted if t.name == interrupt_value["tool"]), None)
            routed = next((n for n, (_, srvs) in AGENTS.items() if server in srvs), None)
        async with SessionLocal() as session:
            approval = Approval(
                run_id=run_id,
                tool_name=interrupt_value["tool"],
                payload=interrupt_value["payload"],
                expires_at=datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours),
            )
            session.add(approval)
            await session.flush()
            run = await session.get(Run, run_id)
            assert run is not None
            run.status = "waiting_approval"
            _accumulate(run, agent_name, routed, tokens_in, tokens_out, elapsed_ms)
            await audit(session, "approval.requested", actor_id=user_id, run_id=run_id,
                        resource=interrupt_value["tool"],
                        detail={"approval_id": str(approval.id), "payload": interrupt_value["payload"]})
            await session.commit()
        await publish({"type": "approval_required", "approval_id": str(approval.id),
                       "tool": interrupt_value["tool"], "payload": interrupt_value["payload"]})
        return

    if status_override and status == "completed":
        status = status_override
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.status = status
        run.output = output
        run.finished_at = datetime.now(UTC)
        _accumulate(run, agent_name, routed, tokens_in, tokens_out, elapsed_ms)
        metric_agent = run.route or agent_name
        await audit(session, "run.finished", actor_id=user_id, run_id=run_id,
                    detail={"status": status, "tokens_in": run.tokens_in, "tokens_out": run.tokens_out})
        await session.commit()

    telemetry.RUNS_TOTAL.labels(agent=metric_agent, status=status).inc()
    telemetry.RUN_LATENCY.labels(agent=metric_agent).observe(elapsed_ms / 1000)
    telemetry.TOKENS_TOTAL.labels(direction="in").inc(tokens_in)
    telemetry.TOKENS_TOTAL.labels(direction="out").inc(tokens_out)

    await publish({"type": "done", "status": status, "output": output,
                   "tokens_in": tokens_in, "tokens_out": tokens_out, "latency_ms": elapsed_ms})


def _accumulate(run: Run, agent_name: str, routed: str | None,
                tokens_in: int, tokens_out: int, elapsed_ms: int) -> None:
    run.agent = agent_name
    if routed:
        run.route = routed
    elif agent_name != "auto" and run.route is None:
        run.route = agent_name
    run.model = llm.resolve()["chat"]
    run.tokens_in += tokens_in
    run.tokens_out += tokens_out
    run.latency_ms = (run.latency_ms or 0) + elapsed_ms
    if run.trace_id is None:
        run.trace_id = current_trace_id()
