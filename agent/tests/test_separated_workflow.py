"""Stored OCR and AI classification have independent execution boundaries."""

import asyncio
import copy

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import ComparisonInputError, LocalPorts, LocalStore
from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.tasks import AgentTask
from agent.services import document_ocr, local_ocr
from agent.services.local_json import write_json
from agent.tests.fixtures_support import pdf_bytes


class Classifier:
    def __init__(self):
        self.inputs = []

    async def invoke(self, operation, data):
        self.inputs.append(copy.deepcopy(data))
        quote = data["pages"][0]
        value = quote.split()[1]
        return {
            "document_type": "payslip",
            "fields": [
                {
                    "field_id": "gross",
                    "raw_value": value,
                    "normalized_value": value,
                    "unit": "KRW",
                    "page": 1,
                    "quote": quote,
                    "issues": [],
                }
            ],
            "issues": [],
        }, {}


def store_at(tmp_path, provider=None):
    return LocalStore(
        AgentSettings(retry_delay_seconds=0),
        state=tmp_path / "state",
        corpus=tmp_path / "corpus",
        provider=provider,
    )


async def finish(store, case_id, stage):
    job = await store.create_job(case_id, stage)
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    return store.jobs[job["id"]]


async def test_ocr_executes_without_model_then_ai_reuses_saved_ocr(tmp_path, monkeypatch):
    provider = Classifier()
    store = store_at(tmp_path, provider)
    calls = []

    def recognize(*args):
        calls.append(args[2])
        return [{"text": "gross: 100 KRW", "lines": []}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    case = await store.upload(pdf_bytes(["Scanned document"], scanned=True), "auto")
    with pytest.raises(ComparisonInputError, match="OCR 추출"):
        await store.create_job(case["id"], "extract")
    with pytest.raises(ComparisonInputError, match="AI 처리"):
        await store.confirm(case["id"])
    ocr_job = await finish(store, case["id"], "ocr")
    assert calls == [[0]]
    assert provider.inputs == []
    assert ocr_job["usage"] == ocr_job["call_budget"] == {}
    assert ocr_job["task"] is None
    ocr = (await store.get_case(case["id"]))["ocr"]
    assert ocr["id"] == ocr_job["id"]
    assert ocr["pages"] == ["gross: 100 KRW"]
    assert ocr["content_hash"] == store.documents[case["document_id"]]["sha256"]

    def forbidden(*args):
        pytest.fail("AI processing must reuse the saved OCR without running OCR")

    monkeypatch.setattr(local_ocr, "recognize_pdf", forbidden)
    restored = store_at(tmp_path, provider)
    ai_job = await finish(restored, case["id"], "extract")
    assert ai_job["task"]["payload"]["ocr_result_id"] == ocr_job["id"]
    result = await restored.get_case(case["id"])
    assert result["document_type"] == "payslip"
    assert result["extraction"]["recognized_pages"] == ocr["pages"]
    assert len(provider.inputs) == 1
    assert provider.inputs[0]["source_method"] == "python_ocr"
    assert "pdf_bytes" not in provider.inputs[0]


@pytest.mark.parametrize(
    "key,value",
    [
        ("document_id", "another-document"),
        ("document_version", 2),
        ("content_hash", "changed"),
    ],
)
async def test_ai_rejects_ocr_bound_to_another_source(tmp_path, monkeypatch, key, value):
    provider = Classifier()
    store = store_at(tmp_path, provider)
    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: [{"text": "gross: 100 KRW", "lines": []}])
    case = await store.upload(pdf_bytes(["Document"]), "payslip")
    await finish(store, case["id"], "ocr")
    store.cases[case["id"]]["ocr"][key] = value
    with pytest.raises(ComparisonInputError, match="문서"):
        await store.create_job(case["id"], "extract")
    assert provider.inputs == []


async def test_read_ocr_checks_result_identity_again_before_ai(tmp_path, monkeypatch):
    store = store_at(tmp_path, Classifier())
    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: [{"text": "gross: 100 KRW", "lines": []}])
    case = await store.upload(pdf_bytes(["Document"]), "payslip")
    await finish(store, case["id"], "ocr")
    job = await store.create_job(case["id"], "extract")
    # The task has not yielded yet, so its input is fixed while the stored result changes.
    internal = store.jobs[job["id"]]
    task = AgentTask.model_validate(internal["task"])
    ports = LocalPorts(store, internal)
    ports.bind(task.context)
    ref = store.document_ref(case["document_id"])
    ports.add_source(ref, store.document_data(case["document_id"]))
    with pytest.raises(IntegrityError, match="저장된 OCR"):
        await ports.read_ocr(task.context, ref, "wrong-result-id")
    await store.tasks[job["id"]]


async def test_new_ocr_invalidates_ai_and_results_while_preserving_working_input(tmp_path, monkeypatch):
    provider = Classifier()
    store = store_at(tmp_path, provider)
    recognized = {"text": "gross: 100 KRW", "lines": []}
    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: [recognized])
    uploaded = await store.upload(pdf_bytes(["Document"], scanned=True), "payslip")
    await finish(store, uploaded["id"], "ocr")
    await finish(store, uploaded["id"], "extract")
    case = store.cases[uploaded["id"]]
    case["draft"]["manual"]["gross"] = "150"
    old_draft = copy.deepcopy(case["draft"])
    old_baseline = copy.deepcopy(case["extraction_draft"])
    case.update(
        confirmed=copy.deepcopy(old_draft),
        confirmed_revision=case["revision"],
        analysis={"result_id": "previous"},
        explanation={"old": True},
        report={"old": True},
    )
    recognized = {"text": "gross: 200 KRW", "lines": []}
    new_ocr_job = await finish(store, case["id"], "ocr")
    result = await store.get_case(case["id"])
    assert result["ocr"]["id"] == new_ocr_job["id"]
    assert result["extraction"] is result["confirmed"] is result["confirmed_revision"] is None
    assert result["dirty"] and result["stale"]
    assert result["report"] is result["explanation"] is None
    assert result["draft"] == old_draft
    assert result["extraction_draft"] == old_baseline
    with pytest.raises(ComparisonInputError, match="AI 처리"):
        await store.confirm(case["id"])
    await finish(store, case["id"], "extract")
    result = await store.get_case(case["id"])
    assert result["draft"]["manual"]["gross"] == "150"
    assert result["extraction_draft"]["manual"]["gross"] == "200"


async def test_ocr_cancel_resume_and_same_case_job_exclusion(tmp_path, monkeypatch):
    store = store_at(tmp_path, Classifier())
    case = await store.upload(pdf_bytes(["Document"], scanned=True), "payslip")
    started = asyncio.Event()

    async def wait_for_cancel(data, settings, control=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(document_ocr, "recognize_document", wait_for_cancel)
    job = await store.create_job(case["id"], "ocr")
    await asyncio.wait_for(started.wait(), timeout=2)
    for stage in ("ocr", "extract"):
        with pytest.raises(ValueError, match="현재 작업"):
            await store.create_job(case["id"], stage)
    cancelled = await store.cancel_job(job["id"])
    assert cancelled["status"] == "cancelled"
    assert not cancelled["usage_may_be_incomplete"]
    assert (await store.get_case(case["id"]))["ocr"] is None

    async def recognize(data, settings, control=None):
        await control()
        return {
            "pages": ["gross: 100 KRW"],
            "page_count": 1,
            "image_pages": [False],
            "ocr_pages": [{"text": "gross: 100 KRW", "lines": []}],
        }

    monkeypatch.setattr(document_ocr, "recognize_document", recognize)
    await store.resume_job(job["id"])
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed"
    assert (await store.get_case(case["id"]))["ocr"]["id"] == job["id"]


async def test_uploaded_documents_persist_without_loading_sample_manifest(tmp_path):
    corpus = tmp_path / "corpus"
    write_json(corpus / "manifest.json", {"documents": [{"id": "sample-document"}]})
    store = store_at(tmp_path, Classifier())
    assert await store.list_documents() == []
    case = await store.upload(pdf_bytes(["Actual upload"]), filename="uploaded.pdf")
    restored = store_at(tmp_path, Classifier())
    documents = await restored.list_documents()
    assert [item["id"] for item in documents] == [case["document_id"]]
    assert documents[0]["title"] == "uploaded.pdf"
    assert "evaluation_status" not in documents[0]


@pytest.mark.parametrize("scanned", [False, True])
async def test_ocr_does_not_construct_default_model_provider(tmp_path, monkeypatch, scanned):
    from agent import runner

    def forbidden(*args, **kwargs):
        pytest.fail("The OCR stage must not construct an AI provider")

    monkeypatch.setattr(runner, "OpenAIProvider", forbidden)
    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: [{"text": "gross: 100 KRW", "lines": []}])
    store = store_at(tmp_path)
    assert store.settings.openai_api_key is None
    case = await store.upload(pdf_bytes(["Document"], scanned=scanned), "payslip")
    await finish(store, case["id"], "ocr")
    assert (await store.get_case(case["id"]))["ocr"]["pages"] == (
        ["gross: 100 KRW"] if scanned else ["Document"]
    )


async def test_interrupted_ocr_restores_as_resumable_after_restart(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    case = await store.upload(pdf_bytes(["Document"]), "payslip")
    started = asyncio.Event()

    async def interrupted(data, settings, control=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(document_ocr, "recognize_document", interrupted)
    job = await store.create_job(case["id"], "ocr")
    await asyncio.wait_for(started.wait(), timeout=2)
    await store.cancel_job(job["id"])
    # A process crash leaves the last persisted state as running.
    store.jobs[job["id"]]["status"] = "running"
    store.save_job(store.jobs[job["id"]])
    restored = store_at(tmp_path)
    assert restored.jobs[job["id"]]["status"] == "interrupted"

    async def recognize(data, settings, control=None):
        await control()
        return {
            "pages": ["gross: 100 KRW"],
            "page_count": 1,
            "image_pages": [False],
            "ocr_pages": [{"text": "gross: 100 KRW", "lines": []}],
        }

    monkeypatch.setattr(document_ocr, "recognize_document", recognize)
    await restored.resume_job(job["id"])
    await restored.tasks[job["id"]]
    assert restored.jobs[job["id"]]["status"] == "completed"
    assert (await restored.get_case(case["id"]))["ocr"]["id"] == job["id"]
