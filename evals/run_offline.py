"""Offline evaluation suite. `python -m evals.run_offline --gate` exits 1 below thresholds.

Assertions (facts, citations, routing, interrupts) are the gate; LLM-judge scores add a
graded quality signal. Results are persisted to eval_runs / eval_results.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import llm
from app.agents import tools as toolbind
from app.db import SessionLocal
from app.models import EvalCase, EvalResult, EvalRun, User
from app.services.runs import build_agent
from evals.judges import judge

DATASETS = Path(__file__).parent / "datasets"
THRESHOLDS = yaml.safe_load((Path(__file__).parent / "thresholds.yaml").read_text())
SUITES = ("docs_qa", "analyst", "routing", "actions")


def load_cases(suite: str) -> list[dict]:
    lines = (DATASETS / f"{suite}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def case_id(suite: str, text: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eval:{suite}:{text}")


def normalize(text: str) -> str:
    return text.replace(",", "").replace(" ", " ")


def contains_score(answer: str, groups: list[list[str]]) -> float:
    normalized = normalize(answer)
    hits = sum(any(alt in normalized or alt in answer for alt in group) for group in groups)
    return hits / len(groups) if groups else 1.0


def _usage(messages) -> int:
    return sum(
        (m.usage_metadata or {}).get("input_tokens", 0) + (m.usage_metadata or {}).get("output_tokens", 0)
        for m in messages if getattr(m, "usage_metadata", None)
    )


async def eval_docs_qa(agent, case: dict) -> dict:
    result = await agent.ainvoke({"messages": [("user", case["input"])]})
    messages = result["messages"]
    answer = messages[-1].content or ""
    context = "\n\n".join(str(m.content) for m in messages if m.type == "tool")[:6000]
    expected = case["expected"]
    faithfulness = await judge("faithfulness", question=case["input"], context=context, answer=answer)
    return {
        "scores": {
            "contains": contains_score(answer, expected["must_contain"]),
            "cited_source": 1.0 if expected["source"] in answer else 0.0,
            "faithfulness": faithfulness["score"],
        },
        "tokens": _usage(messages),
        "answer": answer,
    }


async def eval_analyst(agent, case: dict) -> dict:
    result = await agent.ainvoke({"messages": [("user", case["input"])]})
    messages = result["messages"]
    answer = messages[-1].content or ""
    quality = await judge("report_quality", question=case["input"], answer=answer)
    return {
        "scores": {
            "contains": contains_score(answer, case["expected"]["must_contain"]),
            "report_quality": quality["score"],
        },
        "tokens": _usage(messages),
        "answer": answer,
    }


async def eval_routing(supervisor, case: dict) -> dict:
    """Stops at the first handoff — we only measure the routing decision.

    Streams with subgraphs=True: the supervisor's transfer_to_* tool call only shows up
    inside its own subgraph events (handoff messages are not added to the history).
    """
    routed = None
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    async for event in supervisor.astream(
        {"messages": [("user", case["input"])]}, config, stream_mode="updates", subgraphs=True
    ):
        _, update_map = event if isinstance(event, tuple) else ((), event)
        for update in (update_map or {}).values():
            messages = update.get("messages", []) if isinstance(update, dict) else []
            for msg in messages:
                for tc in getattr(msg, "tool_calls", []):
                    if tc["name"].startswith("transfer_to_"):
                        routed = tc["name"].removeprefix("transfer_to_")
        if routed:
            break
    return {
        "scores": {"route_match": 1.0 if routed == case["expected"]["route"] else 0.0},
        "tokens": 0,
        "answer": f"routed to: {routed}",
    }


async def eval_actions(agent, case: dict) -> dict:
    """Safety invariant: the critical tool must ALWAYS pause for approval.

    Runs through the SUPERVISOR (the production path): covers routing + handoff +
    tool call + interrupt propagation through the subgraph, not just the direct agent.
    """
    interrupted = False
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    async for event in agent.astream(
        {"messages": [("user", case["input"])]}, config, stream_mode="updates"
    ):
        if "__interrupt__" in event:
            interrupted = True
            break
    return {
        "scores": {"interrupt": 1.0 if interrupted else 0.0},
        "tokens": 0,
        "answer": f"interrupted: {interrupted}",
    }


async def run_suite(suite: str, granted, actor_id) -> tuple[dict, list[dict]]:
    cases = load_cases(suite)
    rid = uuid.uuid4()  # synthetic run id for tool audit rows
    if suite in ("routing", "actions"):
        agent = await build_agent("auto", granted, run_id=rid, actor_id=actor_id,
                                  checkpointer=MemorySaver())
        runner = eval_routing if suite == "routing" else eval_actions
    else:
        agent = await build_agent(suite, granted, run_id=rid, actor_id=actor_id, checkpointer=None)
        runner = {"docs_qa": eval_docs_qa, "analyst": eval_analyst}[suite]

    results = []
    for case in cases:
        t0 = time.monotonic()
        try:
            outcome = await runner(agent, case)
        except Exception as exc:
            outcome = {"scores": {k: 0.0 for k in THRESHOLDS[suite]}, "tokens": 0,
                       "answer": f"ERROR {type(exc).__name__}: {exc}"}
        outcome["latency_ms"] = int((time.monotonic() - t0) * 1000)
        outcome["case"] = case
        results.append(outcome)
        print(f"  [{suite}] {case['input'][:60]:60s} {outcome['scores']}")

    keys = sorted({k for r in results for k in r["scores"]})
    aggregate = {k: round(sum(r["scores"].get(k, 0.0) for r in results) / len(results), 3) for k in keys}
    return aggregate, results


async def persist(suite: str, aggregate: dict, results: list[dict], git_sha: str) -> None:
    thresholds = THRESHOLDS[suite]
    async with SessionLocal() as session:
        eval_run = EvalRun(suite=suite, kind="offline", git_sha=git_sha, model=llm.resolve()["chat"])
        session.add(eval_run)
        await session.flush()
        for r in results:
            cid = case_id(suite, r["case"]["input"])
            await session.execute(
                pg_insert(EvalCase)
                .values(id=cid, suite=suite, input=r["case"]["input"], expected=r["case"]["expected"])
                .on_conflict_do_nothing(index_elements=["id"])
            )
            passed = all(r["scores"].get(k, 0.0) >= v for k, v in thresholds.items()
                         if k in r["scores"])
            session.add(EvalResult(eval_run_id=eval_run.id, case_id=cid, scores=r["scores"],
                                   passed=passed, tokens=r["tokens"], latency_ms=r["latency_ms"]))
        await session.commit()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="all", choices=("all", *SUITES))
    parser.add_argument("--gate", action="store_true", help="exit 1 if any threshold fails")
    args = parser.parse_args()

    try:
        git_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True).stdout.strip() or "dev"
    except OSError:
        git_sha = "dev"

    async with SessionLocal() as session:
        admin = (await session.execute(select(User).where(User.role == "admin"))).scalars().first()
        granted = await toolbind.granted_tools(session, "admin")
    if admin is None or not granted:
        sys.exit("no admin user / grants — run `make seed` first")

    suites = SUITES if args.suite == "all" else (args.suite,)
    failures = []
    print(f"model={llm.resolve()['chat']}  git={git_sha}")
    for suite in suites:
        print(f"\n== {suite} ==")
        aggregate, results = await run_suite(suite, granted, admin.id)
        await persist(suite, aggregate, results, git_sha)
        for metric, threshold in THRESHOLDS[suite].items():
            value = aggregate.get(metric, 0.0)
            mark = "PASS" if value >= threshold else "FAIL"
            print(f"  {metric:15s} {value:.3f} (>= {threshold}) {mark}")
            if value < threshold:
                failures.append(f"{suite}.{metric}={value} < {threshold}")

    if failures:
        print("\nGATE FAILED:\n  " + "\n  ".join(failures))
        return 1 if args.gate else 0
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
