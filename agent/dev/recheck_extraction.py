"""Run AI processing on saved OCR, or revalidate a retained response without extra calls.

Stop the local server first: LocalStore supports one process at a time.
"""

import argparse
import asyncio
import json
from pathlib import Path

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.schemas.public.errors import BudgetExceeded


class RevalidationOnlyProvider:
    async def invoke(self, operation, data):
        raise BudgetExceeded("Recovery only revalidates stored outputs; new model calls are disabled")


async def main(args):
    settings = AgentSettings.from_env(
        Path("agent/.env"),
        provider_mode="openai",
        max_stage_calls=args.stage_call_limit if args.stage_call_limit else (3 if args.job_id else 1),
    )
    store = LocalStore(settings, provider=RevalidationOnlyProvider() if args.job_id else None)
    if args.job_id:
        job = await store.resume_job(args.job_id)
    else:
        job = await store.create_job(args.case_id, "extract")
    print(json.dumps({"job_id": job["id"], "status": job["status"]}), flush=True)
    await store.tasks[job["id"]]
    job = await store.get_job(job["id"])
    print(json.dumps(job, ensure_ascii=True, indent=2), flush=True)
    case = await store.get_case(job["case_id"])
    if case["extraction"]:
        print(json.dumps(case["extraction"], ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--case-id")
    target.add_argument("--job-id")
    parser.add_argument("--stage-call-limit", type=int, choices=[1, 2, 3])
    asyncio.run(main(parser.parse_args()))
