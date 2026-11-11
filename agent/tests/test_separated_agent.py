"""The AI task consumes completed OCR; it cannot start an OCR process."""

from copy import deepcopy

import pytest

from agent import run_task
from agent.config import AgentSettings
from agent.runtime import Runtime
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import context, pdf_bytes, source


def completed_ocr_task():
    data = pdf_bytes(["gross: 2000000 KRW"])
    ref = source(data)
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(source=ref, document_type="payslip", ocr_result_id="test-ocr-1"),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    pages = [{"text": "gross: 2000000 KRW", "lines": []}]
    ports.ocr_results["test-ocr-1"] = {
        "id": "test-ocr-1",
        "document_id": ref.document_id,
        "document_version": ref.document_version,
        "content_hash": ref.content_hash,
        "pages": [pages[0]["text"]],
        "ocr_pages": pages,
        "page_count": 1,
        "image_pages": [False],
    }
    return task, ports


class CountingProvider(FixtureProvider):
    calls = 0

    async def invoke(self, operation, inputs):
        self.calls += 1
        return await super().invoke(operation, inputs)


def forbid_ocr(*args):
    pytest.fail("AI processing must only read the stored OCR result")


async def test_ai_reads_completed_ocr_and_never_invokes_the_ocr_engine(monkeypatch):
    monkeypatch.setattr("agent.services.local_ocr.recognize_pdf", forbid_ocr)
    task, ports = completed_ocr_task()
    original = deepcopy(ports.ocr_results)
    provider = CountingProvider()
    outcome = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert outcome.status == "completed", outcome.error
    assert outcome.payload.fields[0].normalized_value == "2000000"
    assert outcome.payload.recognized_pages == ["gross: 2000000 KRW"]
    assert provider.calls == 1
    assert ports.ocr_results == original


async def test_no_completed_ocr_identifier_stops_before_model_and_ocr_calls(monkeypatch):
    monkeypatch.setattr("agent.services.local_ocr.recognize_pdf", forbid_ocr)
    task, ports = completed_ocr_task()
    task.payload.ocr_result_id = None
    provider = CountingProvider()
    outcome = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert outcome.status == "failed" and outcome.error.code == "OCR_REQUIRED"
    assert provider.calls == 0


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", "another-ocr-result", "INPUT_INTEGRITY_INVALID"),
        ("document_id", "another-document", "INPUT_INTEGRITY_INVALID"),
        ("document_version", 2, "INPUT_INTEGRITY_INVALID"),
        ("content_hash", "changed", "INPUT_INTEGRITY_INVALID"),
        ("page_count", 2, "INPUT_INTEGRITY_INVALID"),
        ("pages", [""], "OCR_REQUIRED"),
        ("ocr_pages", [{"text": "other source text", "lines": []}], "OCR_REQUIRED"),
    ],
)
async def test_changed_or_invalid_stored_ocr_never_reaches_ai(monkeypatch, field, value, code):
    monkeypatch.setattr("agent.services.local_ocr.recognize_pdf", forbid_ocr)
    task, ports = completed_ocr_task()
    ports.ocr_results["test-ocr-1"][field] = value
    provider = CountingProvider()
    outcome = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert outcome.status == "failed" and outcome.error.code == code
    assert provider.calls == 0


def test_new_ocr_identifier_invalidates_the_old_ai_checkpoint():
    task, ports = completed_ocr_task()
    original = Runtime(task, ports, AgentSettings(), FixtureProvider()).fingerprint
    task.payload.ocr_result_id = "test-ocr-2"
    assert Runtime(task, ports, AgentSettings(), FixtureProvider()).fingerprint != original
