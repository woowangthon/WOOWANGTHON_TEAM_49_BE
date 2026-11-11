"""Small opt-in API evaluation using only synthetic, non-personal documents."""

import argparse
import asyncio
import json
from pathlib import Path

from agent import run_task
from agent.config import AgentSettings
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import (
    analysis_fixture,
    confirmed_result,
    context,
    explanation_task,
    pdf_bytes,
    source,
)

SAMPLES = [
    (
        "contract",
        False,
        ["SYNTHETIC CONTRACT - NOT REAL", "hourly_rate: 10000 KRW/hour", "daily_minutes: 480 minute"],
        {"10000", "480"},
    ),
    (
        "attendance",
        False,
        ["SYNTHETIC ATTENDANCE - NOT REAL", "worked_minutes: 480 minute", "overtime_minutes: 60 minute"],
        {"480", "60"},
    ),
    (
        "payslip",
        False,
        ["SYNTHETIC PAYSLIP - NOT REAL", "gross: 2000000 KRW", "deduction: 200000 KRW", "net: 1800000 KRW"],
        {"2000000", "200000", "1800000"},
    ),
    ("deposit", False, ["SYNTHETIC DEPOSIT - NOT REAL", "deposit_amount: 1800000 KRW"], {"1800000"}),
    ("attendance", True, ["SYNTHETIC SCANNED ATTENDANCE", "worked_minutes: 480 minute"], {"480"}),
]


async def evaluate(output, env_path, sample_index=None, include_explanation=True):
    output.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings.from_env(env_path, provider_mode="openai")
    ports = FakePorts(directory=output / "private")
    records = []
    expected_records = (len(SAMPLES) if sample_index is None else 1) + int(include_explanation)

    def save():
        totals = {}
        for record in records:
            for key, value in record.get("usage", {}).items():
                totals[key] = totals.get(key, 0) + value
        report = {
            "model": settings.extraction_model,
            "explanation_model": settings.explanation_model,
            "model_config_version": settings.model_config_version,
            "records": records,
            "usage": totals,
            "release_ready": False,
            "all_passed": len(records) == expected_records and all(r["passed"] for r in records),
        }
        (output / "evaluation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    for index, (kind, scanned, lines, expected) in enumerate(SAMPLES):
        if sample_index is not None and index != sample_index:
            continue
        data = pdf_bytes(lines, scanned=scanned, font_path=settings.font_path)
        ref = source(data, source_id=f"live-source-{index}", status="pending")
        ports.add_source(ref, data)
        task = AgentTask(
            context=context("extract_document", job_id=f"live-extract-{index}"),
            payload=ExtractionInput(
                ocr_result_id="test-ocr-1",
                source=ref,
                document_type=kind,
                model_config_version=settings.model_config_version,
            ),
        )
        ports.bind(task.context)
        outcome = await run_task(task, ports=ports, settings=settings)
        found = (
            {f.normalized_value for f in outcome.payload.fields} if outcome.status == "completed" else set()
        )
        passed = outcome.status == "completed" and expected <= found
        records.append(
            {
                "sample": f"{kind}-{'scan' if scanned else 'text'}",
                "status": outcome.status,
                "passed": passed,
                "expected_values_found": len(expected & found),
                "expected_values": len(expected),
                "usage": outcome.usage,
                "error_code": outcome.error.code if outcome.error else None,
                "model_calls": sum(
                    v
                    for k, v in ports.checkpoints.get(task.context.job_id).call_budget.items()
                    if not k.endswith(":format_failures")
                )
                if task.context.job_id in ports.checkpoints
                else 0,
            }
        )
        (output / f"sample-{index}.json").write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
        save()
        if outcome.status != "completed":
            # Do not bill the remaining campaign after an account/model/network failure.
            print(json.dumps(save(), ensure_ascii=True))
            return False
    if not include_explanation:
        report = save()
        print(json.dumps(report, ensure_ascii=True))
        return report["all_passed"]
    analysis, ref, data = analysis_fixture()
    analysis.context.job_id = "live-analysis-fixture"
    ports.bind(analysis.context)
    ports.add_source(ref, data)
    result = await run_task(analysis, ports=ports, settings=AgentSettings())
    if result.status != "completed":
        raise RuntimeError("Synthetic confirmed-result preparation failed")
    task = explanation_task(confirmed_result(result.payload), job_id="live-explanation")
    task.payload.model_config_version = settings.model_config_version
    ports.bind(task.context)
    outcome = await run_task(task, ports=ports, settings=settings)
    passed = outcome.status == "completed" and outcome.payload.status == "generated"
    records.append(
        {
            "sample": "explanation",
            "status": outcome.status,
            "passed": passed,
            "usage": outcome.usage,
            "error_code": outcome.error.code if outcome.error else None,
            "model_calls": sum(
                v
                for k, v in ports.checkpoints[task.context.job_id].call_budget.items()
                if not k.endswith(":format_failures")
            ),
        }
    )
    (output / "explanation.json").write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
    report = save()
    print(json.dumps(report, ensure_ascii=True))
    return report["all_passed"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path("agent/.env"))
    parser.add_argument("--output", type=Path, default=Path("output/live-evaluation"))
    parser.add_argument("--sample-index", type=int, choices=range(5))
    parser.add_argument("--skip-explanation", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        0 if asyncio.run(evaluate(args.output, args.env, args.sample_index, not args.skip_explanation)) else 1
    )


if __name__ == "__main__":
    main()
