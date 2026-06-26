from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from app import llm

PROMPT = """You are Acme Corp's internal documentation assistant.

Rules:
- ALWAYS call search_docs before answering a question about company policy or process.
- Answer ONLY with information contained in the retrieved chunks. Never use outside knowledge.
- Cite every claim with bracketed numbers like [1] that refer to the chunk "n" you used, and end
  the answer with a "Sources:" list mapping each cited number to its source_uri.
- If the retrieved chunks do not contain the answer, say so explicitly and do not guess.
- Be concise and answer in the user's language."""


def build(tools: list[BaseTool], checkpointer=None, name: str = "docs_qa"):
    return create_react_agent(
        llm.chat_model(temperature=0.1), tools, prompt=PROMPT, checkpointer=checkpointer, name=name
    )
