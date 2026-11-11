"""Automatic OCR document classification is persisted through the local workflow."""

import json

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.services import local_ocr
from agent.tests.fixtures_support import pdf_bytes


@pytest.mark.parametrize(
    "actual_type,field_id,value,unit,draft_group,draft_key",
    [
        ("contract", "contract_rate", "12000", "KRW/hour", "values", "contract_rate"),
        ("attendance", "worked_minutes", "480", "minute", "manual", "worked_minutes"),
        ("payslip", "gross", "2000000", "KRW", "manual", "gross"),
        ("deposit", "deposit_total", "1800000", "KRW", "manual", "deposit"),
    ],
)
@pytest.mark.parametrize("scanned", [False, True])
async def test_auto_upload_resolves_type_draft_and_saved_ocr(
    tmp_path, monkeypatch, actual_type, field_id, value, unit, draft_group, draft_key, scanned
):
    text = f"{field_id}: {value} {unit}"
    data = pdf_bytes([text], scanned=scanned)
    ocr_calls, model_calls = [], []

    def recognize(pdf, settings, indices):
        assert pdf == data
        ocr_calls.append(indices)
        return [{"text": text, "lines": [{"text": text, "confidence": 99}]}]

    class Provider:
        async def invoke(self, operation, inputs):
            model_calls.append(operation)
            assert ocr_calls == ([[0]] if scanned else [])
            assert inputs["document_type"] == "auto"
            assert inputs["source_method"] == ("python_ocr" if scanned else "pdf_text")
            assert inputs["pages"] == [text]
            assert "pdf_bytes" not in inputs
            return {
                "document_type": actual_type,
                "fields": [
                    {
                        "field_id": field_id,
                        "raw_value": value,
                        "normalized_value": value,
                        "unit": unit,
                        "page": 1,
                        "quote": text,
                        "issues": [],
                    }
                ],
                "issues": [],
            }, {}

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    settings = AgentSettings(retry_delay_seconds=0)
    state, corpus = tmp_path / "state", tmp_path / "corpus"
    store = LocalStore(settings, state=state, corpus=corpus, provider=Provider())
    original = await store.upload(data, "auto", filename="synthetic.pdf")
    assert original["document_type"] == "auto"
    job = await store.create_job(original["id"], "extract")
    await store.tasks[job["id"]]
    result = await store.get_case(original["id"])
    assert model_calls == ["extract"]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    assert store.jobs[job["id"]]["document_type"] == "auto"
    assert store.jobs[job["id"]]["resolved_document_type"] == actual_type
    assert result["document_type"] == actual_type
    assert result["extraction"]["document_type"] == actual_type
    assert result["draft"][draft_group][draft_key] == value
    assert result["extraction"]["recognized_pages"] == [text]
    assert result["extraction"]["verification_method"] == ("ocr_unverified" if scanned else "text_match")
    assert not result["confirmed"]

    saved = json.loads((state / "cases" / f"{result['id']}.json").read_text(encoding="utf-8"))
    assert saved["document_type"] == actual_type
    assert saved["extraction"]["recognized_pages"] == [text]
    restored = LocalStore(settings, state=state, corpus=corpus, provider=Provider())
    assert await restored.get_case(result["id"]) == result
    assert restored.jobs[job["id"]]["resolved_document_type"] == actual_type
    assert restored.jobs[job["id"]]["status"] == "completed"
    assert model_calls == ["extract"], "Restoring saved state must not call the model"


async def test_unresolved_auto_type_fails_without_changing_case_or_draft(tmp_path, monkeypatch):
    text = "Unsupported or mixed document contents"
    monkeypatch.setattr(
        local_ocr,
        "recognize_pdf",
        lambda *args: [{"text": text, "lines": [{"text": text, "confidence": 99}]}],
    )

    class Provider:
        calls = 0

        async def invoke(self, operation, inputs):
            self.calls += 1
            assert inputs["document_type"] == "auto"
            assert inputs["pages"] == [text]
            assert "pdf_bytes" not in inputs
            return {"document_type": "unknown", "fields": [], "tables": [], "issues": ["UNSUPPORTED"]}, {}

    provider = Provider()
    settings = AgentSettings(retry_delay_seconds=0)
    state, corpus = tmp_path / "state", tmp_path / "corpus"
    store = LocalStore(settings, state=state, corpus=corpus, provider=provider)
    case = await store.upload(pdf_bytes([text]), "auto")
    original_draft = case["draft"]
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    failed = store.jobs[job["id"]]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "MODEL_OUTPUT_INVALID"
    assert "문서 유형을 확정하지 못했습니다" in failed["error"]
    assert "resolved_document_type" not in failed
    assert provider.calls == 1  # An unresolved type requires user input, not a format-repair call.
    result = await store.get_case(case["id"])
    assert result["document_type"] == "auto"
    assert result["extraction"] is None
    assert result["draft"] == original_draft
    restored = LocalStore(settings, state=state, corpus=corpus, provider=provider)
    assert (await restored.get_case(case["id"]))["document_type"] == "auto"
    assert restored.jobs[job["id"]]["status"] == "failed"
