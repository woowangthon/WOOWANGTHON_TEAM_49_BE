import asyncio

import pytest

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import (
    DependencyUnavailable,
    ExecutionRevoked,
    ModelTransientError,
)
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import context, pdf_bytes, source


def extraction_task(data=None):
    data = data or pdf_bytes(["SYNTHETIC PAYSLIP", "gross: 2000000 KRW"])
    ref = source(data, status="pending")
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type="payslip"),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    return task, ports, AgentSettings(retry_delay_seconds=0)


async def test_fixture_extraction_has_grounded_quote():
    task, ports, settings = extraction_task()
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    assert out.payload.fields[0].normalized_value == "2000000"
    assert out.payload.verification_method == "ocr_unverified"
    assert "gross: 2000000 KRW" in out.payload.recognized_pages[0]
    assert out.payload.evidence_catalog[0].page == 1
    assert sum(ports.checkpoints[task.context.job_id].call_budget.values()) == 1


@pytest.mark.parametrize("mutation", ["page", "quote", "normalized", "duplicate"])
def test_extraction_rejects_invented_evidence(mutation):
    item = {
        "field_id": "gross",
        "raw_value": "100",
        "normalized_value": "100",
        "unit": "KRW",
        "page": 1,
        "quote": "gross: 100 KRW",
        "issues": [],
    }
    response = {"document_type": "payslip", "fields": [item], "issues": []}
    if mutation == "page":
        item["page"] = 2
    elif mutation == "quote":
        item["quote"] = "invented: 100 KRW"
    elif mutation == "normalized":
        item["normalized_value"] = "200"
    else:
        response["fields"].append(dict(item))
    checked = verify_response(response, ["gross: 100 KRW"], "payslip")
    assert len(checked.fields) == 1
    item = checked.fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert item.validation_reasons
    if mutation in ("page", "quote"):
        assert not item.evidence_valid


async def test_damage_and_size_limits_do_not_call_model():
    for data, limit in [(b"not a PDF", 10000), (pdf_bytes(["hello"]), 1)]:
        task, ports, settings = extraction_task(data)
        settings.max_file_bytes = limit
        out = await run_task(task, ports=ports, settings=settings)
        assert out.status == "failed"
        assert out.error.code == "DOCUMENT_UNREADABLE"
        assert not ports.checkpoints.get(task.context.job_id)


async def test_model_retries_budget_survives_new_execution():
    class FailingProvider:
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            raise ModelTransientError()

    provider = FailingProvider()
    task, ports, settings = extraction_task()
    out = await run_task(task, ports=ports, settings=settings, provider=provider)
    assert out.status == "failed"
    assert provider.calls == 3
    task.context.run_id = "run-2"
    task.context.execution_token = "new-token"
    ports.bind(task.context)
    out = await run_task(task, ports=ports, settings=settings, provider=provider)
    assert out.status == "failed" and out.error.code == "MODEL_BUDGET_EXHAUSTED"
    assert provider.calls == 3


async def test_format_repair_at_most_once():
    class BadProvider:
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            return {"wrong": "schema"}, {}

    provider = BadProvider()
    task, ports, settings = extraction_task()
    out = await run_task(task, ports=ports, settings=settings, provider=provider)
    assert out.status == "failed"
    assert provider.calls == 2


async def test_checkpoint_storage_failure_prevents_paid_call():
    class FailingStorage(FakePorts):
        async def save_checkpoint(self, context, checkpoint):
            raise DependencyUnavailable()

    class CountingProvider(FixtureProvider):
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            return await super().invoke(operation, data)

    task, old, settings = extraction_task()
    ports = FailingStorage()
    ports.bind(task.context)
    ports.add_source(task.payload.source, old.sources[task.payload.source.source_id])
    provider = CountingProvider()
    out = await run_task(task, ports=ports, settings=settings, provider=provider)
    assert out.error.code == "DEPENDENCY_UNAVAILABLE"
    assert provider.calls == 0


async def test_resume_from_disk_without_recomputing_nodes(setup_agent, tmp_path):
    task, old, settings = setup_agent
    ports = FakePorts(directory=tmp_path)
    ports.bind(task.context)
    for ref in task.payload.snapshot.authorized_sources:
        ports.add_source(ref, old.sources[ref.source_id])
    first = await run_task(task, ports=ports, settings=settings)
    fresh = FakePorts(directory=tmp_path)
    task.context.run_id = "run-2"
    task.context.execution_token = "new-token"
    fresh.bind(task.context)
    for ref in task.payload.snapshot.authorized_sources:
        fresh.add_source(ref, old.sources[ref.source_id])
    second = await run_task(task, ports=fresh, settings=settings)
    assert first.status == second.status == "completed"
    assert first.payload == second.payload
    assert fresh.events == []


async def test_stale_execution_cannot_write(setup_agent):
    task, ports, settings = setup_agent
    await run_task(task, ports=ports, settings=settings)
    newer = task.context.model_copy(update={"run_id": "run-new"}, deep=True)
    newer.execution_token = "new-token"
    ports.bind(newer)
    with pytest.raises(ExecutionRevoked):
        await ports.save_checkpoint(task.context, ports.checkpoints[task.context.job_id])
    with pytest.raises(ExecutionRevoked):
        await ports.stage_artifact(task.context, b"late", "application/pdf")


@pytest.mark.parametrize("revocation", ["cancel", "access", "token"])
async def test_cancellation_during_model_call_discards_response(revocation):
    task, ports, settings = extraction_task()

    class RevokingProvider(FixtureProvider):
        async def invoke(self, operation, data):
            response = await super().invoke(operation, data)
            if revocation == "cancel":
                ports.cancelled.add(task.context.job_id)
            elif revocation == "access":
                ports.revoked.add(task.payload.source.source_id)
            else:
                ports.current_tokens[task.context.job_id] = "replacement"
            return response

    out = await run_task(task, ports=ports, settings=settings, provider=RevokingProvider())
    assert out.status == "cancelled"
    assert out.payload is None


async def test_checkpoint_tampering_and_incompatible_version(setup_agent):
    task, ports, settings = setup_agent
    await run_task(task, ports=ports, settings=settings)
    cp = ports.checkpoints[task.context.job_id]
    cp.payload_hash = "changed"
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"


async def test_parallel_save_collects_all_four_workers(setup_agent):
    task, ports, settings = setup_agent

    class SlowPorts(FakePorts):
        async def save_checkpoint(self, context, checkpoint):
            await asyncio.sleep(0.001)
            await super().save_checkpoint(context, checkpoint)

    slow = SlowPorts()
    slow.bind(task.context)
    for ref in task.payload.snapshot.authorized_sources:
        slow.add_source(ref, ports.sources[ref.source_id])
    out = await run_task(task, ports=slow, settings=settings)
    assert out.status == "completed"
    assert {"worker_wage", "worker_terms", "worker_statement", "worker_payment"} <= set(
        slow.checkpoints[task.context.job_id].payload
    )


def test_synthetic_fixture_pdf_is_reproducible():
    from agent.tests.fixtures_support import analysis_fixture

    first, _, first_bytes = analysis_fixture()
    second, _, second_bytes = analysis_fixture()
    assert first_bytes == second_bytes
    assert first.payload.snapshot_hash == second.payload.snapshot_hash


async def test_demo_can_resume_after_rebuilding_fixtures(tmp_path):
    from agent.tests.support.run_fixture import demo

    await demo(tmp_path)
    first_pdf = (tmp_path / "summary.pdf").read_bytes()
    await demo(tmp_path)
    assert (tmp_path / "summary.pdf").read_bytes() == first_pdf
