import io
import json
import zipfile

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfReader

from agent import run_task
from agent.graphs.explanation_graph import guard_response
from agent.schemas.public.errors import ModelFormatError
from agent.schemas.public.tasks import AgentOutcome, AgentTask, ReportInput
from agent.services.hashing import content_hash, result_hash
from agent.tests.fixtures_support import confirmed_result, context, explanation_task


async def prepare(setup_agent):
    task, ports, settings = setup_agent
    analysis = await run_task(task, ports=ports, settings=settings)
    assert analysis.status == "completed"
    result = confirmed_result(analysis.payload)
    explanation = explanation_task(result)
    ports.bind(explanation.context)
    out = await run_task(explanation, ports=ports, settings=settings)
    return result, out, ports, settings


async def test_explanation_uses_only_bound_facts(setup_agent):
    result, out, _, _ = await prepare(setup_agent)
    assert out.status == "completed"
    assert out.payload.result_hash == result.result_hash
    assert out.payload.status == "generated"
    assert "{{" not in "".join(out.payload.paragraphs)


@pytest.mark.parametrize(
    "template", ["체불 확정", "100원을 지급해야 합니다.", "{{invented}}", "{{summary}} 회사 잘못입니다."]
)
def test_unapproved_explanation_is_blocked(template):
    with pytest.raises(ModelFormatError):
        guard_response({"templates": [template], "question_ids": []}, {"summary": "요약"}, [])


async def test_explanation_failure_falls_back_without_changing_result(setup_agent):
    result, _, ports, settings = await prepare(setup_agent)

    class BadProvider:
        async def invoke(self, operation, data):
            return {"templates": ["불법입니다."], "question_ids": data["question_ids"]}, {}

    task = explanation_task(result, job_id="bad-explanation")
    ports.bind(task.context)
    out = await run_task(task, ports=ports, settings=settings, provider=BadProvider())
    assert out.status == "completed"
    assert out.payload.status == "template_fallback"
    assert result_hash(result) == result.result_hash


async def test_pdf_zip_hangul_originals_and_hashes(setup_agent):
    result, explanation, ports, settings = await prepare(setup_agent)
    ref = next(iter(ports.source_refs.values()))
    task = AgentTask(
        context=context("build_report"),
        payload=ReportInput(
            result=result,
            explanation=explanation.payload,
            explanation_version="exp-v1",
            selected_sources=[ref],
            generated_at=context("build_report").requested_at,
        ),
    )
    ports.bind(task.context)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    pdf, archive = [ports.artifacts[a.artifact_id] for a in out.payload.artifacts]
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "월급잇다" in text and result.result_id in text
    assert "검토 필요" in text
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert z.testzip() is None
        assert z.read("summary.pdf") == pdf
        manifest = json.loads(z.read("manifest.json"))
        assert len(manifest) == 1
        for path, expected in manifest.items():
            assert content_hash(z.read(path)) == expected == ref.content_hash


async def test_cross_result_explanation_rejected(setup_agent):
    result, explanation, ports, settings = await prepare(setup_agent)
    explanation.payload.result_version = 9
    task = AgentTask(
        context=context("build_report"),
        payload=ReportInput(
            result=result,
            explanation=explanation.payload,
            explanation_version="exp-v1",
            generated_at=context("build_report").requested_at,
        ),
    )
    ports.bind(task.context)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
    assert not ports.artifacts


async def test_cross_case_confirmed_result_rejected(setup_agent):
    result, _, ports, settings = await prepare(setup_agent)
    task = explanation_task(result, job_id="cross-case")
    task.context.case_id = "another-case"
    ports.bind(task.context)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"


async def test_public_schemas_and_examples(setup_agent):
    task, ports, settings = setup_agent
    schema = AgentTask.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(task.model_dump_json()))
    out = await run_task(task, ports=ports, settings=settings)
    schema = AgentOutcome.model_json_schema(mode="serialization")
    Draft202012Validator(schema).validate(json.loads(out.model_dump_json()))
    assert "fixture-execution-token" not in task.model_dump_json()
    assert not any(
        f.unit == "KRW" and isinstance(f.value, float) for f in task.payload.snapshot.confirmed_fields
    )
