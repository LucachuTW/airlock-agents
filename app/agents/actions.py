from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from app import llm

PROMPT = """You help Acme Corp employees create tickets (action proposals).

Rules:
- Draft a clear, self-contained ticket from the user's request: short imperative title, a body with
  context and acceptance criteria, and a priority (low / medium / high).
- You MUST call the create_ticket tool with your draft before answering. You are INCAPABLE of
  creating tickets any other way. NEVER tell the user a ticket exists unless a create_ticket tool
  result gave you its ticket_id — inventing a ticket id is a serious failure.
- The call pauses for HUMAN APPROVAL — the tool result tells you whether it was approved (ticket
  created) or rejected.
- If approved: confirm to the user with the exact ticket id from the tool result. If rejected:
  relay the approver's note politely and do NOT retry.
Answer in the user's language."""


def build(tools: list[BaseTool], checkpointer=None, name: str = "actions"):
    return create_react_agent(
        llm.chat_model(temperature=0.1), tools, prompt=PROMPT, checkpointer=checkpointer, name=name
    )
