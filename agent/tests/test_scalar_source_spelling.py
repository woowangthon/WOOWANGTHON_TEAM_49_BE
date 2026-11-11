"""Recover OCR scalar typography without borrowing another quantity or unit."""

from copy import deepcopy

import pytest

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import context, pdf_bytes, source


def response_for(raw, quote, *, field_id="break_minutes", value="60", unit="minute"):
    return {
        "document_type": "contract",
        "fields": [
            {
                "field_id": field_id,
                "raw_value": raw,
                "normalized_value": value,
                "unit": unit,
                "page": 1,
                "quote": quote,
                "issues": [],
            }
        ],
        "issues": [],
    }


@pytest.mark.parametrize(
    "raw,quote,field_id,value,unit,source_raw",
    [
        (
            "60 minutes",
            "5. Recess hours ( 60 )minutes per day",
            "break_minutes",
            "60",
            "minute",
            "( 60 )minutes",
        ),
        ("60분", "휴게시간: 60 분", "break_minutes", "60", "minute", "60 분"),
        (
            "25일",
            "8. 임 금 지 급 일 매월 25 일 지급.",
            "payday",
            "25",
            "day_of_month",
            "25 일",
        ),
        ("8hours", "1일 ( 8 ) hours", "scheduled_daily_minutes", "480", "minute", "( 8 ) hours"),
        ("60분", "휴게60분간", "break_minutes", "60", "minute", "60분"),
    ],
)
def test_only_scalar_typography_is_recovered(raw, quote, field_id, value, unit, source_raw):
    response = response_for(raw, quote, field_id=field_id, value=value, unit=unit)
    original = deepcopy(response)
    verified = verify_response(response, [quote], "contract")
    assert verified.fields[0].raw_value == source_raw
    assert verified.fields[0].normalized_value == value
    assert verified.fields[0].quote == quote
    assert ("SOURCE_SCALAR_SPELLING_RESTORED" in verified.fields[0].issues) == (raw != source_raw)
    assert response == original


@pytest.mark.parametrize(
    "raw,quote,field_id,value,unit",
    [
        ("60 minutes", "Recess ( 160 )minutes per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess ( 1.60 )minutes per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess ( -60 )minutes per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess - ( 60 )minutes per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess - 60 minutes per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess ( 60 )hours per day", "break_minutes", "60", "minute"),
        ("60 minutes", "Recess ( 60 )minutes per week", "break_minutes", "60", "minute"),
        ("1 hour", "Recess ( 1 )hour 30 minutes", "break_minutes", "60", "minute"),
        ("30 minutes", "Recess 1 hour ( 30 )minutes", "break_minutes", "30", "minute"),
        ("60", "Recess 160 minutes", "break_minutes", "60", "minute"),
        ("60", "Recess - 60 minutes", "break_minutes", "60", "minute"),
        ("25일", "지급일 125 일", "payday", "25", "day_of_month"),
        ("25일", "지급일 2.25 일", "payday", "25", "day_of_month"),
        ("25일", "지급일 25 월", "payday", "25", "day_of_month"),
    ],
)
def test_scalar_recovery_never_borrows_partial_negative_or_other_unit(raw, quote, field_id, value, unit):
    checked = verify_response(
        response_for(raw, quote, field_id=field_id, value=value, unit=unit), [quote], "contract"
    )
    item = checked.fields[0]
    assert item.validation_status == "needs_review"
    assert item.normalized_value is None and item.suggested_value == value
    assert "SOURCE_VALUE_MISMATCH" in item.validation_reasons


def test_recovery_cannot_make_a_fabricated_quote_valid():
    response = response_for("60 minutes", "Recess ( 60 )minutes per day")
    item = verify_response(response, ["Recess ( 30 )minutes per day"], "contract").fields[0]
    assert item.validation_status == "needs_review" and not item.evidence_valid
    assert item.normalized_value is None
    assert "SOURCE_QUOTE_MISMATCH" in item.validation_reasons


def test_recovery_still_verifies_normalized_quantity():
    quote = "Recess ( 60 )minutes per day"
    item = verify_response(response_for("60 minutes", quote, value="3600"), [quote], "contract").fields[0]
    assert item.normalized_value is None and item.suggested_value == "3600"
    assert "SOURCE_NORMALIZATION_INVALID" in item.validation_reasons


async def test_packaged_candidate_keeps_recovered_ocr_spelling_without_another_ai_call(monkeypatch):
    quote = "5. Recess hours ( 60 )minutes per day"
    response = response_for("60 minutes", quote)
    monkeypatch.setattr("agent.services.local_ocr.recognize_pdf", lambda *_: [{"text": quote, "lines": []}])
    data = pdf_bytes([quote], scanned=True)
    ref = source(data)
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type="auto"),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)

    class RecordedProvider:
        calls = 0

        async def invoke(self, operation, inputs):
            self.calls += 1
            return deepcopy(response), {"total_tokens": 13}

    provider = RecordedProvider()
    outcome = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert outcome.status == "completed", outcome.error
    assert provider.calls == 1
    assert outcome.usage == {"total_tokens": 13}
    assert outcome.payload.fields[0].raw_value == "( 60 )minutes"
    assert outcome.payload.fields[0].normalized_value == "60"
    assert "SOURCE_SCALAR_SPELLING_RESTORED" in outcome.payload.fields[0].issues
    assert outcome.payload.evidence_catalog[0].quote == quote
    assert outcome.payload.verification_method == "ocr_unverified"
