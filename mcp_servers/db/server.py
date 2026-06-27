"""MCP server `db`: read-only SQL over the analytics schema.

Trust boundary: sqlglot validation (single SELECT, allowlisted schemas, no DML anywhere,
including inside CTEs) + a `mcp_readonly` Postgres role that only has SELECT on `sales`,
+ read-only transactions, statement timeout and a hard row cap.
"""

import json
import os
from decimal import Decimal

import psycopg
import sqlglot
from mcp.server.fastmcp import FastMCP
from sqlglot import exp

mcp = FastMCP("db")

RO_URL = os.environ.get("MCP_DB_URL", "postgresql://mcp_readonly:mcp_readonly@localhost:5432/agentic")
ALLOWED_SCHEMAS = set(os.environ.get("MCP_DB_SCHEMAS", "sales").split(","))
MAX_ROWS = 500
TIMEOUT_MS = 10_000

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.TruncateTable, exp.Grant, exp.Command, exp.Set, exp.Lock,
)


def validate_sql(sql: str) -> str | None:
    """Return an error message, or None if the statement is an allowlisted SELECT."""
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as e:
        return f"SQL syntax error: {e}"
    if len(statements) != 1 or statements[0] is None:
        return "Exactly one statement is allowed"
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        return "Only SELECT statements are allowed"
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN):
            return f"Forbidden construct: {type(node).__name__}"
    cte_names = {cte.alias_or_name for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if isinstance(table.this, exp.Func):  # table functions like generate_series
            continue
        schema = table.db or ""
        if not schema and table.name in cte_names:  # reference to a CTE defined above
            continue
        if schema not in ALLOWED_SCHEMAS:
            return (
                f"Table '{table.sql()}' not allowed — qualify tables with an allowed schema: "
                f"{sorted(ALLOWED_SCHEMAS)}"
            )
    return None


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    return v if isinstance(v, (int, float, str, bool, type(None))) else str(v)


@mcp.tool()
def list_tables() -> str:
    """List queryable tables: columns, types and the table's data dictionary comment."""
    with psycopg.connect(RO_URL) as conn:
        rows = conn.execute(
            """
            select c.table_schema, c.table_name, c.column_name, c.data_type,
                   obj_description(format('%%I.%%I', c.table_schema, c.table_name)::regclass)
            from information_schema.columns c
            where c.table_schema = any(%s)
            order by c.table_schema, c.table_name, c.ordinal_position
            """,
            (sorted(ALLOWED_SCHEMAS),),
        ).fetchall()
    out: dict[str, dict] = {}
    for schema, table, column, dtype, comment in rows:
        entry = out.setdefault(f"{schema}.{table}", {"comment": comment, "columns": []})
        entry["columns"].append({"column": column, "type": dtype})
    return json.dumps(out)


@mcp.tool()
def run_query(sql: str) -> str:
    """Run a single read-only SELECT (tables must be schema-qualified, e.g. sales.orders).

    Returns JSON {columns, rows, truncated}; at most 500 rows.
    """
    if error := validate_sql(sql):
        return json.dumps({"error": error})
    try:
        with psycopg.connect(
            RO_URL, options=f"-c statement_timeout={TIMEOUT_MS} -c default_transaction_read_only=on"
        ) as conn:
            cur = conn.execute(sql)
            columns = [d.name for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
    except psycopg.Error as e:
        return json.dumps({"error": str(e).strip()})
    truncated = len(rows) > MAX_ROWS
    return json.dumps(
        {
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows[:MAX_ROWS]],
            "truncated": truncated,
        }
    )


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
