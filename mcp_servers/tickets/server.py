"""MCP server `tickets`: the only write-capable tool, gated by human approval.

The server verifies at the boundary that:
  1. the approval exists, is for create_ticket, and was APPROVED by a human;
  2. the payload being executed is exactly the payload the human saw;
  3. execution is idempotent (unique ticket per approval).
The agent can therefore never execute anything that wasn't literally approved.
"""

import json
import os

import psycopg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tickets")

DB_URL = os.environ.get("MCP_DATABASE_URL", "postgresql://agentic:agentic@localhost:5432/agentic")
PRIORITIES = {"low", "medium", "high"}


@mcp.tool()
def create_ticket(title: str, body: str, priority: str, approval_id: str) -> str:
    """Create a ticket. Requires the id of a human approval covering this exact payload."""
    if priority not in PRIORITIES:
        return json.dumps({"error": f"priority must be one of {sorted(PRIORITIES)}"})
    with psycopg.connect(DB_URL) as conn:
        row = conn.execute(
            "select status, tool_name, payload from approvals where id = %s", (approval_id,)
        ).fetchone()
        if row is None:
            return json.dumps({"error": "unknown approval_id"})
        status, tool_name, payload = row
        if tool_name != "create_ticket" or status != "approved":
            return json.dumps({"error": f"approval is not an approved create_ticket (status={status})"})
        if payload != {"title": title, "body": body, "priority": priority}:
            return json.dumps({"error": "payload does not match the approved payload"})
        created = conn.execute(
            """
            insert into tickets (approval_id, title, body, priority)
            values (%s, %s, %s, %s)
            on conflict (approval_id) do nothing
            returning id
            """,
            (approval_id, title, body, priority),
        ).fetchone()
        if created is None:  # idempotent retry: return the existing ticket
            created = conn.execute(
                "select id from tickets where approval_id = %s", (approval_id,)
            ).fetchone()
        conn.commit()
    return json.dumps({"ticket_id": str(created[0]), "status": "created", "priority": priority})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
