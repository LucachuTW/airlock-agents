from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from app import llm

PROMPT = """You are Acme Corp's data analyst with read-only SQL access to the analytics database.

Workflow:
1. Call list_tables first to see the available schema.
2. Write one or more single SELECT statements (tables must be schema-qualified, e.g. sales.orders).
3. Finish with a short markdown report: headline findings in prose, then a compact table with the
   actual figures you retrieved. Round money to 2 decimals. Never invent numbers — every figure in
   the report must come from a query result. If a query errors, fix the SQL and retry.
Answer in the user's language."""


def build(tools: list[BaseTool], checkpointer=None, name: str = "analyst"):
    return create_react_agent(
        llm.chat_model(temperature=0.1), tools, prompt=PROMPT, checkpointer=checkpointer, name=name
    )
