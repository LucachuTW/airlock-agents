"""Supervisor: routes each request to the right specialist subagent."""

from langgraph_supervisor import create_supervisor

from app import llm

DESCRIPTIONS = {
    "docs_qa": "questions about company policies, documentation and internal processes",
    "analyst": "data questions, reports and metrics from the sales analytics database",
    "actions": "requests to create tickets or propose actions that change something",
}

PROMPT = """You are the supervisor of Acme Corp's assistant team. Route each user request to
exactly ONE specialist by calling its handoff tool:
{agents}

Rules:
- Do not answer domain questions yourself — always delegate to the matching specialist.
- After the specialist finishes, return its answer to the user unchanged.
- Only for pure greetings or smalltalk, answer directly and briefly."""


def build(subagents: list, checkpointer=None):
    lines = "\n".join(f"- {a.name}: {DESCRIPTIONS[a.name]}" for a in subagents)
    workflow = create_supervisor(
        agents=subagents,
        model=llm.chat_model(temperature=0),
        prompt=PROMPT.format(agents=lines),
        output_mode="last_message",
        # Small local models get derailed by "Successfully transferred to X" tool messages
        # in their context (they answer as dispatchers instead of acting). Keep the
        # subagent's view identical to the direct-agent path, which is eval-verified.
        add_handoff_messages=False,
        add_handoff_back_messages=False,
    )
    return workflow.compile(checkpointer=checkpointer)
