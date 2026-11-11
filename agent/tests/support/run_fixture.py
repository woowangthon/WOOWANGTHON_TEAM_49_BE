import argparse
import asyncio
import json
from pathlib import Path

from agent import run_task as _run_task
from agent.config import AgentSettings
from agent.rules.registry import RuleRegistry
from agent.rules.support_manifest import support_manifest
from agent.schemas.public.ports import CheckpointEnvelope, ProgressEvent
from agent.schemas.public.results import ArtifactRef
from agent.schemas.public.tasks import AgentOutcome, AgentTask, ExtractionInput, ReportInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import analysis_fixture, confirmed_result, context, explanation_task


async def run_task(task, **options):
    return await _run_task(task, provider=FixtureProvider(), **options)


async def demo(directory):
    ports = FakePorts(directory=directory / "private")
    task, ref, data = analysis_fixture()
    ports.add_source(ref, data)
    ports.bind(task.context)
    settings = AgentSettings()
    analysis = await run_task(task, ports=ports, settings=settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "analysis.json").write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    if analysis.status != "completed":
        raise RuntimeError(analysis.error.code)
    confirmed = confirmed_result(analysis.payload)  # DEMO ONLY: production backend owns this step.
    explain = explanation_task(confirmed)
    ports.bind(explain.context)
    explanation = await run_task(explain, ports=ports, settings=settings)
    if explanation.status != "completed":
        raise RuntimeError(explanation.error.code)
    report = AgentTask(
        context=context("build_report"),
        payload=ReportInput(
            result=confirmed,
            explanation=explanation.payload,
            explanation_version="explanation-1",
            selected_sources=[ref],
            generated_at=task.context.requested_at,
        ),
    )
    ports.bind(report.context)
    report_outcome = await run_task(report, ports=ports, settings=settings)
    if report_outcome.status != "completed":
        raise RuntimeError(report_outcome.error.code)
    for artifact in report_outcome.payload.artifacts:
        name = "summary.pdf" if artifact.mime_type == "application/pdf" else "consultation.zip"
        (directory / name).write_bytes(ports.artifact_bytes(artifact))
    (directory / "report.json").write_text(report_outcome.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "completed",
                "output": str(directory.resolve()),
                "release_ready": False,
                "comparisons": len(analysis.payload.comparisons),
            },
            ensure_ascii=False,
        )
    )


def export_contracts(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for model in (AgentTask, AgentOutcome, CheckpointEnvelope, ProgressEvent, ArtifactRef):
        (directory / f"{model.__name__}.schema.json").write_text(
            json.dumps(
                model.model_json_schema(mode="serialization" if model is AgentOutcome else "validation"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    task, _, _ = analysis_fixture()
    (directory / "analyze_case.example.json").write_text(task.model_dump_json(indent=2), encoding="utf-8")
    (directory / "support_manifest.json").write_text(
        json.dumps(support_manifest(RuleRegistry()), indent=2), encoding="utf-8"
    )


async def export_examples(directory):
    export_contracts(directory)
    ports = FakePorts()
    settings = AgentSettings()
    analysis, ref, data = analysis_fixture()
    ports.add_source(ref, data)
    ports.bind(analysis.context)
    outcome = await run_task(analysis, ports=ports, settings=settings)
    result = confirmed_result(outcome.payload)
    extraction = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type="payslip"),
    )
    explain = explanation_task(result)
    ports.bind(explain.context)
    explained = await run_task(explain, ports=ports, settings=settings)
    report = AgentTask(
        context=context("build_report"),
        payload=ReportInput(
            result=result,
            explanation=explained.payload,
            explanation_version="example-v1",
            selected_sources=[ref],
            generated_at=analysis.context.requested_at,
        ),
    )
    for task in [analysis, extraction, explain, report]:
        ports.bind(task.context)
        out = await run_task(task, ports=ports, settings=settings)
        if out.status != "completed":
            raise RuntimeError("Contract example generation failed: " + out.error.code)
        name = task.context.task_kind
        (directory / f"{name}.task.json").write_text(task.model_dump_json(indent=2), encoding="utf-8")
        (directory / f"{name}.outcome.json").write_text(out.model_dump_json(indent=2), encoding="utf-8")
    bad = analysis.model_copy(deep=True)
    bad.context.schema_version = "unsupported"
    failed = await run_task(bad, ports=ports, settings=settings)
    (directory / "unsupported-version.task.json").write_text(bad.model_dump_json(indent=2), encoding="utf-8")
    (directory / "unsupported-version.outcome.json").write_text(
        failed.model_dump_json(indent=2), encoding="utf-8"
    )
    ports.cancelled.add(analysis.context.job_id)
    cancelled = await run_task(analysis, ports=ports, settings=settings)
    (directory / "cancelled.outcome.json").write_text(cancelled.model_dump_json(indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["demo", "schemas", "check-release"])
    parser.add_argument("--output", type=Path, default=Path("output/agent-demo-0.1.0"))
    args = parser.parse_args()
    if args.command == "demo":
        asyncio.run(demo(args.output))
    elif args.command == "schemas":
        asyncio.run(export_examples(args.output))
    else:
        print(json.dumps(support_manifest(RuleRegistry()), indent=2))
        raise SystemExit(1)  # Correctly fails until independent wage-rule coverage is verified.


if __name__ == "__main__":
    main()
