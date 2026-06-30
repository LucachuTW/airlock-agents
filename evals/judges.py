"""LLM-as-judge with the local tier model. Prompts are in-repo and diffable.

Known limitation (documented): on small tiers the judge is the same model as the agent
(self-judging). Assertions (exact facts, citations, routing, interrupts) carry the gate;
judge scores add a graded signal on top.
"""

import json

from app import llm

TEMPLATES = {
    "faithfulness": (
        "You are a strict evaluator. Rate how FAITHFUL the ANSWER is to the CONTEXT "
        "(retrieved documentation): 5 = every claim is directly supported; 3 = mostly "
        "supported with minor unsupported detail; 1 = contradicts or invents facts. "
        'Respond with ONLY JSON: {"score": <1-5>, "reason": "<one short sentence>"}'
    ),
    "relevance": (
        "You are a strict evaluator. Rate how well the ANSWER addresses the QUESTION: "
        "5 = complete and direct; 3 = partial; 1 = off-topic or refuses without reason. "
        'Respond with ONLY JSON: {"score": <1-5>, "reason": "<one short sentence>"}'
    ),
    "report_quality": (
        "You are a strict evaluator of data-analysis reports. Rate the ANSWER: 5 = concrete "
        "figures, clear structure, includes a table; 3 = has figures but poorly presented; "
        "1 = vague, no actual numbers. "
        'Respond with ONLY JSON: {"score": <1-5>, "reason": "<one short sentence>"}'
    ),
}


async def judge(kind: str, **fields: str) -> dict:
    """Returns {"score": 0..1, "reason": str}. Score 0 when the judge output is unparseable."""
    model = llm.chat_model(temperature=0, format="json")
    prompt = TEMPLATES[kind] + "\n\n" + "\n\n".join(
        f"{name.upper()}:\n{value}" for name, value in fields.items()
    )
    response = await model.ainvoke(prompt)
    try:
        data = json.loads(response.content)
        score = max(1.0, min(5.0, float(data["score"])))
        return {"score": round(score / 5, 3), "reason": str(data.get("reason", ""))[:300]}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"score": 0.0, "reason": f"unparseable judge output: {response.content[:120]}"}
