"""OCR-to-classification boundaries, using synthetic documents and model spies."""

import io

import pytest
from pypdf import PdfReader, PdfWriter

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.runtime import Runtime
from agent.schemas.public.errors import ModelFormatError, OCRFailed, OCRUnavailable
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.services import local_ocr
from agent.tests.fake_ports import FakePorts
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import context, pdf_bytes, source


def task_for(document_type, data):
    ref = source(data, status="pending")
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type=document_type),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    return task, ports


def model_result(document_type, field_id="gross", raw="100", unit="KRW", page=1):
    return {
        "document_type": document_type,
        "fields": [
            {
                "field_id": field_id,
                "raw_value": raw,
                "normalized_value": raw,
                "unit": unit,
                "page": page,
                "quote": f"{field_id}: {raw} {unit}",
                "issues": [],
            }
        ],
        "issues": [],
    }


@pytest.mark.parametrize("scanned", [False, True])
@pytest.mark.parametrize(
    "document_type,field_id,raw,unit",
    [
        ("contract", "contract_rate", "12000", "KRW/hour"),
        ("attendance", "worked_minutes", "480", "minute"),
        ("payslip", "gross", "2000000", "KRW"),
        ("deposit", "deposit_total", "1800000", "KRW"),
    ],
)
async def test_every_document_is_ocr_before_ai_even_with_text_layer(
    monkeypatch, scanned, document_type, field_id, raw, unit
):
    events = []
    recognized = f"{field_id}: {raw} {unit}"
    data = pdf_bytes(["PDF TEXT MUST NOT REPLACE OCR"], scanned=scanned)

    def recognize(pdf, settings, indices):
        assert pdf == data
        events.append(("ocr", indices))
        return [{"text": recognized, "lines": []}]

    class Provider:
        async def invoke(self, operation, inputs):
            assert events == [("ocr", [0])]
            events.append(("ai", operation))
            assert inputs["source_method"] == "python_ocr"
            assert inputs["pages"] == [recognized]
            assert "PDF TEXT MUST NOT REPLACE OCR" not in inputs["page_text"]
            assert "pdf_bytes" not in inputs
            return model_result(document_type, field_id, raw, unit), {}

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    task, ports = task_for(document_type, data)
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=Provider())
    assert out.status == "completed", out.error
    assert out.payload.recognized_pages == [recognized]
    assert out.payload.verification_method == "ocr_unverified"
    assert all(ref.verification_method == "ocr_unverified" for ref in out.payload.evidence_catalog)
    assert "OCR_REQUIRES_USER_CONFIRMATION" in out.payload.issues


@pytest.mark.parametrize(
    "ocr_result,code",
    [
        (OCRUnavailable("OCR 엔진 설치가 필요합니다."), "OCR_UNAVAILABLE"),
        (OCRFailed("OCR 처리 시간이 초과되었습니다."), "OCR_FAILED"),
        ([{"text": "  ", "lines": []}], "OCR_FAILED"),
        ([], "OCR_FAILED"),
        (None, "OCR_FAILED"),
        ([{"lines": []}], "OCR_FAILED"),
    ],
)
async def test_unavailable_empty_or_invalid_ocr_never_calls_ai(monkeypatch, ocr_result, code):
    def recognize(*args):
        if isinstance(ocr_result, Exception):
            raise ocr_result
        return ocr_result

    class ForbiddenProvider:
        async def invoke(self, *args):
            pytest.fail("OCR failure must stop before model invocation")

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    task, ports = task_for("contract", pdf_bytes(["Readable PDF text is not an OCR fallback"]))
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=ForbiddenProvider())
    assert out.status == "failed"
    assert out.error.code == code


async def test_all_pages_are_ocr_in_order_before_ai(monkeypatch):
    writer = PdfWriter()
    for text in ["Text layer page one", "Text layer page two"]:
        writer.add_page(PdfReader(io.BytesIO(pdf_bytes([text]))).pages[0])
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    seen = []
    texts = ["gross: 100 KRW", "net: 100 KRW"]

    def recognize(pdf, settings, indices):
        seen.extend(indices)
        return [{"text": texts[indices[0]], "lines": []}]

    class Provider:
        async def invoke(self, operation, inputs):
            assert seen == [0, 1]
            assert inputs["pages"] == texts
            return model_result("payslip", "net", "100", "KRW", page=2), {}

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    task, ports = task_for("payslip", data)
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=Provider())
    assert out.status == "completed", out.error
    assert out.payload.page_count == 2
    assert out.payload.recognized_pages == texts
    assert out.payload.evidence_catalog[0].page == 2


def test_model_cannot_cite_an_empty_ocr_page():
    item = verify_response(model_result("payslip", page=2), ["gross: 100 KRW", ""], "payslip").fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert not item.evidence_valid and "SOURCE_QUOTE_MISMATCH" in item.validation_reasons


async def test_auto_classification_returns_actual_type_and_its_fields(monkeypatch):
    text = "deposit_total: 1800000 KRW"
    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: [{"text": text, "lines": []}])

    class Provider:
        async def invoke(self, operation, inputs):
            assert inputs["document_type"] == "auto"
            assert inputs["pages"] == [text]
            return model_result("deposit", "deposit_total", "1800000", "KRW"), {}

    task, ports = task_for("auto", pdf_bytes(["Ignored PDF text"]))
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=Provider())
    assert out.status == "completed", out.error
    assert out.payload.document_type == "deposit"
    assert out.payload.fields[0].field_id == "deposit_total"


def test_auto_classification_does_not_accept_unknown_document():
    with pytest.raises(ModelFormatError) as error:
        verify_response({"document_type": "unknown", "fields": [], "tables": [], "issues": []}, ["?"], "auto")
    assert error.value.reason == "DOCUMENT_TYPE_UNRESOLVED"


async def test_low_confidence_ocr_remains_explicitly_unverified(monkeypatch):
    text = "gross: 100 KRW"
    monkeypatch.setattr(
        local_ocr,
        "recognize_pdf",
        lambda *args: [{"text": text, "lines": [{"text": text, "confidence": 42}]}],
    )
    task, ports = task_for("payslip", pdf_bytes(["gross: 100 KRW"]))
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=FixtureProvider())
    assert out.status == "completed", out.error
    assert "OCR_LOW_CONFIDENCE" in out.payload.issues
    assert "PYTHON_OCR_AI_CLASSIFICATION" in out.payload.issues
    assert out.payload.verification_method == "ocr_unverified"


@pytest.mark.parametrize("document_type", ["contract", "attendance", "payslip", "deposit", "auto"])
def test_ocr_settings_changes_invalidate_every_document_checkpoint(document_type):
    task, ports = task_for(document_type, pdf_bytes(["Synthetic document"]))
    first = Runtime(task, ports, AgentSettings(ocr_dpi=150), FixtureProvider())
    changed = Runtime(task, ports, AgentSettings(ocr_dpi=300), FixtureProvider())
    assert first.fingerprint != changed.fingerprint
