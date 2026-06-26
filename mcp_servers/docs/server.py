"""MCP server `docs`: semantic search over the internal documentation corpus.

Runs over stdio in dev (spawned by the API); set MCP_TRANSPORT=streamable-http for K8s.
This process is the trust boundary for doc access: it validates inputs and holds the
(read-only) DB access — the agent never sees a connection string.
"""

import json
import os

import httpx
import psycopg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docs")

DB_URL = os.environ.get("MCP_DATABASE_URL", "postgresql://agentic:agentic@localhost:5432/agentic")
OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")


@mcp.tool()
def search_docs(query: str, k: int = 5) -> str:
    """Semantic search over internal company documentation.

    Returns a JSON list of chunks: [{"n", "content", "source_uri", "title", "score"}].
    Cite sources by their source_uri when answering from these chunks.
    """
    k = max(1, min(int(k), 20))
    resp = httpx.post(f"{OLLAMA}/api/embed", json={"model": EMBED_MODEL, "input": query}, timeout=60)
    resp.raise_for_status()
    vector = json.dumps(resp.json()["embeddings"][0])
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            """
            select c.content, d.source_uri, d.title, 1 - (c.embedding <=> %s::vector) as score
            from chunks c join documents d on d.id = c.document_id
            order by c.embedding <=> %s::vector
            limit %s
            """,
            (vector, vector, k),
        ).fetchall()
    return json.dumps(
        [
            {"n": i + 1, "content": r[0], "source_uri": r[1], "title": r[2], "score": round(r[3], 4)}
            for i, r in enumerate(rows)
        ],
        ensure_ascii=False,
    )


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
