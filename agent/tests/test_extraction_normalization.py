import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import ModelFormatError
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.services.normalization import normalize_evidenced_scalar, normalize_scalar
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import context, pdf_bytes, source


def captured_response():
    """Three fields captured from the synthetic monthly scan's failing response."""
    response = {
        "document_type": "contract",
        "fields": [
            {
                "field_id": "pay_type",
                "raw_value": "월",
                "normalized_value": "monthly",
                "unit": None,
                "page": 1,
                "quote": "1) 월 통상임금: 월 3,000,000원",
                "issues": [],
            },
        ],
        "issues": [],
    }
    response["fields"].extend(
        {
            "field_id": name,
            "raw_value": raw,
            "normalized_value": value,
            "unit": "minute",
            "page": 1,
            "quote": "Mon-Fri / 8 hours per day, 40 hours per week",
            "issues": [],
        }
        for name, raw, value in (
            ("scheduled_daily_minutes", "8", "480"),
            ("scheduled_weekly_minutes", "40", "2400"),
        )
    )
    return response


@pytest.mark.parametrize(
    "raw,unit,quote,expected",
    [
        ("월", "category", "1) 월 통상임금: 월 3,000,000원", "monthly"),
        ("월 통상임금", "category", "월 통상임금: 월 3,000,000원", "monthly"),
        ("8", "minute", "소정근로시간: 1일 8시간, 주 40시간", "480"),
        ("40", "minute", "소정근로시간: 1일 8시간, 주 40시간", "2400"),
        ("8", "minute", "( 8 ) hours per day", "480"),
        ("60", "minute", "( 60 ) minutes per day", "60"),
        ("1.5", "minute", "1.5 hours", "90"),
        ("8", "minute", "8 minutes", "8"),
    ],
)
def test_normalization_uses_only_explicit_quote_context(raw, unit, quote, expected):
    assert normalize_evidenced_scalar(raw, unit, quote) == expected


@pytest.mark.parametrize(
    "raw,unit,quote",
    [
        ("월", "category", "2026년 10월 1일"),
        ("월", "category", "급여 종류: [ ] 월 [ ] 일"),
        ("월", "category", "월 통상임금: ______원"),
        ("8", "minute", "8 hours or 8 minutes"),
        ("1", "minute", "1시간 30분"),
        ("1", "minute", "1 hour 30 minutes"),
    ],
)
def test_ambiguous_or_partial_source_context_is_rejected(raw, unit, quote):
    with pytest.raises(ValueError):
        normalize_evidenced_scalar(raw, unit, quote)


def test_bare_scalar_keeps_minutes_and_does_not_infer_from_other_numbers():
    assert normalize_scalar("8", "minute") == "8"
    assert normalize_evidenced_scalar("8", "minute", "08:00 to 18:00") == "8"
    assert normalize_evidenced_scalar("8", "minute", "80 hours; 1.8 hours") == "8"
    with pytest.raises(ValueError):
        normalize_scalar("월", "category")


def test_captured_model_response_passes_without_editing_source_values():
    response = captured_response()
    original = deepcopy(response)
    verify_response(response, ["\n".join(field["quote"] for field in response["fields"])], "contract")
    assert response == original


@pytest.mark.parametrize("index,bad_value", [(0, "hourly"), (1, "8"), (2, "40")])
def test_incorrect_normalized_values_still_fail(index, bad_value):
    response = captured_response()
    response["fields"][index]["normalized_value"] = bad_value
    checked = verify_response(
        response, ["\n".join(field["quote"] for field in response["fields"])], "contract"
    )
    item = checked.fields[index]
    assert item.validation_status == "needs_review"
    assert item.normalized_value is None and item.suggested_value == bad_value
    assert "SOURCE_NORMALIZATION_INVALID" in item.validation_reasons
    assert all(
        field.validation_status == "valid" for other, field in enumerate(checked.fields) if other != index
    )


async def test_scanned_contract_completes_with_previously_rejected_response(monkeypatch):
    response = captured_response()
    text = "\n".join(field["quote"] for field in response["fields"])
    monkeypatch.setattr(
        "agent.services.local_ocr.recognize_pdf",
        lambda *_: [{"text": text, "lines": [{"text": text, "confidence": 99}]}],
    )
    data = pdf_bytes([field["quote"] for field in response["fields"]], scanned=True)
    ref = source(data)
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type="contract"),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)

    class RecordedProvider:
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            assert operation == "extract" and data["source_method"] == "python_ocr"
            assert "pdf_bytes" not in data
            assert data["pages"] == [text]
            return deepcopy(response), {}

    provider = RecordedProvider()
    outcome = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert outcome.status == "completed", outcome.error
    assert provider.calls == 1
    assert outcome.payload.verification_method == "ocr_unverified"
    assert {f.field_id: f.normalized_value for f in outcome.payload.fields} == {
        "pay_type": "monthly",
        "scheduled_daily_minutes": "480",
        "scheduled_weekly_minutes": "2400",
    }
    assert "OCR_REQUIRES_USER_CONFIRMATION" in outcome.payload.issues


async def test_retry_receives_safe_feedback_and_preserves_usage_and_budget():
    from agent.tests.test_runtime_and_extraction import extraction_task

    task, ports, settings = extraction_task()

    class RepairingProvider:
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            if self.calls == 1:
                assert "validation_feedback" not in data
                raise ModelFormatError(
                    "sensitive source text",
                    reason="SOURCE_NORMALIZATION_INVALID",
                    field_id="scheduled_daily_minutes",
                )
            assert data["validation_feedback"] == {
                "reason": "SOURCE_NORMALIZATION_INVALID",
                "field_id": "scheduled_daily_minutes",
            }
            return {"document_type": "payslip", "fields": [], "issues": []}, {"total_tokens": 5}

    provider = RepairingProvider()
    outcome = await run_task(task, ports=ports, settings=settings, provider=provider)
    assert outcome.status == "completed", outcome.error
    assert outcome.usage == {"total_tokens": 5}
    checkpoint = ports.checkpoints[task.context.job_id]
    assert checkpoint.call_budget == {"extract_candidates": 2, "extract_candidates:format_failures": 1}
    assert "sensitive source text" not in checkpoint.model_dump_json()


def test_untrusted_field_id_is_not_written_to_feedback():
    error = ModelFormatError(field_id="user-provided-private-document-text")
    assert error.field_id is None


@pytest.mark.parametrize(
    "raw,unit,field_id,expected",
    [
        ("1일 8시간", "minute", "scheduled_daily_minutes", "480"),
        ("1주 40시간", "minute", "scheduled_weekly_minutes", "2400"),
        ("하루 1시간 30분", "minute", "scheduled_daily_minutes", "90"),
        ("1일 60분", "minute", "break_minutes", "60"),
        ("8 hours per day", "minute", "scheduled_daily_minutes", "480"),
        ("40 hours per week", "minute", "scheduled_weekly_minutes", "2400"),
        ("8시간/일", "minute", "scheduled_daily_minutes", "480"),
        ("월 3,000,000원", "KRW/month", "contract_rate", "3000000"),
        ("월급 300만원", "KRW/month", "contract_rate", "3000000"),
        ("연봉 3천만원", "KRW/year", "contract_rate", "30000000"),
        ("일급 10만원", "KRW/day", "contract_rate", "100000"),
        ("시간당 15,000원", "KRW/hour", "contract_rate", "15000"),
    ],
)
def test_explicit_period_labels_do_not_change_the_source_quantity(raw, unit, field_id, expected):
    assert normalize_evidenced_scalar(raw, unit, raw, field_id=field_id) == expected


@pytest.mark.parametrize(
    "raw,unit,field_id",
    [
        ("1주 40시간", "minute", "scheduled_daily_minutes"),
        ("1일 8시간", "minute", "scheduled_weekly_minutes"),
        ("2일 16시간", "minute", "scheduled_daily_minutes"),
        ("1일 8", "minute", "scheduled_daily_minutes"),
        ("8시간 또는 9시간", "minute", "scheduled_daily_minutes"),
        ("월 300만원", "KRW/hour", "contract_rate"),
        ("월 300만원 또는 400만원", "KRW/month", "contract_rate"),
    ],
)
def test_period_conflicts_and_ambiguous_quantities_are_rejected(raw, unit, field_id):
    with pytest.raises(ValueError):
        normalize_evidenced_scalar(raw, unit, raw, field_id=field_id)


def test_bare_number_cannot_use_the_other_schedule_period():
    with pytest.raises(ValueError):
        normalize_evidenced_scalar(
            "40", "minute", "소정근로시간: 1일 8시간, 1주 40시간", field_id="scheduled_daily_minutes"
        )


def test_second_real_monthly_scan_response_passes_unchanged():
    path = Path(__file__).parent / "fixtures/monthly_scan_response.json"
    response = json.loads(path.read_text(encoding="utf-8"))
    original = deepcopy(response)
    assert len(response["fields"]) == 10
    pages = [
        "\n".join(field["quote"] for field in response["fields"] if field["page"] == page) for page in (1, 2)
    ]
    verify_response(response, pages, "contract")
    assert response == original


async def test_private_pending_response_recovers_without_another_call_or_budget_reset(tmp_path):
    from agent.tests.test_runtime_and_extraction import extraction_task

    task, ports, settings = extraction_task()
    ports = FakePorts(directory=tmp_path)
    ports.bind(task.context)
    data = pdf_bytes(["SYNTHETIC PAYSLIP", "gross: 2000000 KRW"])
    ports.add_source(task.payload.source, data)
    invalid_response = {
        "document_type": "payslip",
        "fields": [
            {
                "field_id": "gross",
                "raw_value": "2000000",
                "normalized_value": "3000000",
                "unit": "KRW",
                "page": 1,
                "quote": "gross: 2000000 KRW",
                "issues": [],
            }
        ],
        "issues": [],
    }

    class PreviouslyInvalidProvider:
        calls = 0

        async def invoke(self, operation, data):
            self.calls += 1
            return deepcopy(invalid_response), {"total_tokens": 9}

    provider = PreviouslyInvalidProvider()
    # Exercise the runtime's structural-response retry/recovery contract directly.
    # A scalar normalization mismatch now produces a review candidate, not a failed job.
    from agent.runtime import Runtime

    initial = Runtime(task, ports, settings, provider)
    await initial.start([task.payload.source])

    def previous_validator(response):
        raise ModelFormatError("Invalid extraction schema")

    async def original_call():
        return await initial.call_model("extract_candidates", "extract", {}, validator=previous_validator)

    with pytest.raises(ModelFormatError):
        await initial.step("extract_candidates", original_call)
    assert provider.calls == 2
    checkpoint = ports.checkpoints[task.context.job_id]
    assert (
        checkpoint.payload["__pending_model_outputs__"]["extract_candidates"]["response"] == invalid_response
    )
    original_budget = dict(checkpoint.call_budget)

    # Exercise a corrected validator on the same protected checkpoint after process restart.
    restarted = FakePorts(directory=tmp_path)
    restarted.bind(task.context)
    restarted.add_source(task.payload.source, data)
    runtime = Runtime(task, restarted, settings, provider)
    await runtime.start([task.payload.source])

    def corrected_validator(response):
        assert response == invalid_response

    async def revalidate():
        return await runtime.call_model("extract_candidates", "extract", {}, validator=corrected_validator)

    result = await runtime.step("extract_candidates", revalidate)
    assert result["document_type"] == "payslip"
    assert provider.calls == 2
    assert runtime.checkpoint.call_budget == original_budget
    assert runtime.usage == {"total_tokens": 18}
    assert "extract_candidates" not in runtime.checkpoint.payload["__pending_model_outputs__"]
