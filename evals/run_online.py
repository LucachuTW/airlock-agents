"""Online evaluation: judge a sample of recent production runs (no golden answers).

Run periodically (K8s CronJob / cron): `python -m evals.run_online --hours 24 --sample 20`.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import llm
from app.db import SessionLocal
from app.models import EvalResult, EvalRun, Run
from evals.judges import judge


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args()

    async with SessionLocal() as session:
        since = datetime.now(UTC) - timedelta(hours=args.hours)
        already = select(EvalResult.run_id).where(EvalResult.run_id.isnot(None))
        runs = (
            (await session.execute(
                select(Run)
                .where(Run.status == "completed", Run.finished_at >= since, Run.id.not_in(already))
                .order_by(Run.finished_at.desc())
                .limit(args.sample)
            )).scalars().all()
        )
        if not runs:
            print("no unjudged completed runs in window")
            return

        eval_run = EvalRun(suite="online", kind="online", model=llm.resolve()["chat"])
        session.add(eval_run)
        await session.flush()
        for run in runs:
            verdict = await judge("relevance", question=run.input, answer=run.output or "")
            session.add(EvalResult(
                eval_run_id=eval_run.id, run_id=run.id,
                scores={"relevance": verdict["score"], "reason": verdict["reason"]},
                passed=verdict["score"] >= 0.6,
                tokens=run.tokens_in + run.tokens_out, latency_ms=run.latency_ms,
            ))
            print(f"  {str(run.id)[:8]} relevance={verdict['score']:.2f} {verdict['reason'][:60]}")
        await session.commit()
        print(f"judged {len(runs)} runs (eval_run={eval_run.id})")


if __name__ == "__main__":
    asyncio.run(main())
