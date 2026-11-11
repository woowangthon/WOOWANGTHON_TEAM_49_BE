"""Review candidates preserve evidence and isolate errors without another paid call."""

from copy import deepcopy

import pytest

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import ModelFormatError
from agent.schemas.public.tasks import AgentTask, ExtractionInput
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import context, pdf_bytes, source


def candidate(raw="25th", value="25", *, field_id="payday", unit="day_of_month", quote=None):
    return {
        "field_id": field_id,
        "raw_value": raw,
        "normalized_value": value,
        "unit": unit,
        "page": 1,
        "quote": raw if quote is None else quote,
        "issues": [],
    }


def response(*fields):
    return {"document_type": "contract", "fields": list(fields), "issues": []}


@pytest.mark.parametrize(
    "raw", ["25th", "on the 25th", "every month on day 25", "payday: the 25th of each month"]
)
def test_typed_day_candidate_does_not_require_a_sentence_spelling_rule(raw):
    original = response(candidate(raw))
    before = deepcopy(original)
    item = verify_response(original, [raw], "contract").fields[0]
    assert item.validation_status == "valid"
    assert item.normalized_value == item.suggested_value == "25"
    assert item.raw_value == item.source_quote == raw
    assert original == before


@pytest.mark.parametrize(
    "raw,value",
    [
        ("after 25 days", "25"),
        ("within 25", "25"),
        ("25 days", "25"),
        ("25th or last day", "25"),
        ("25th or 26th", "25"),
        ("25%", "25"),
        ("25st", "25"),
        ("32nd", "32"),
        ("125th", "25"),
        ("25th", "26"),
        ("25th", "025"),
        ("twenty-fifth", "25"),
    ],
)
def test_ambiguous_or_contradictory_day_is_review_only(raw, value):
    item = verify_response(response(candidate(raw, value)), [raw], "contract").fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert item.suggested_value == value and item.validation_reasons


@pytest.mark.parametrize("quote", ["Payday: 125th", "Payday: - 25th", "Payday: 2.25th"])
def test_ordinal_cannot_borrow_part_of_a_larger_or_signed_number(quote):
    item = verify_response(response(candidate(quote=quote)), [quote], "contract").fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert "SOURCE_VALUE_MISMATCH" in item.validation_reasons


@pytest.mark.parametrize(
    "quote",
    [
        "Payable on the 25th day after employment ends",
        "Payable within the 25th day",
        "Payable on the 25th day of employment",
    ],
)
def test_short_payday_scalar_cannot_discard_relative_source_context(quote):
    item = verify_response(response(candidate(quote=quote)), [quote], "contract").fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert "SOURCE_NORMALIZATION_INVALID" in item.validation_reasons


@pytest.mark.parametrize(
    "raw,quote",
    [
        ("25th", "OCR segment 252; regular payday: 25th of each month"),
        ("25 일", "매월 25 일 (휴일이면 전일 지급)"),
        ("25th", "25th of each month; if a holiday, pay the day before"),
    ],
)
def test_regular_payday_keeps_independent_ocr_context_and_holiday_adjustments(raw, quote):
    item = verify_response(response(candidate(raw, quote=quote)), [quote], "contract").fields[0]
    assert item.validation_status == "valid" and item.normalized_value == "25"


@pytest.mark.parametrize("kind,prefix", [("contract", "contract"), ("attendance", "period")])
def test_reversed_period_is_reviewed_before_it_reaches_confirmation(kind, prefix):
    value = response(
        candidate("2026-11-30", "2026-11-30", field_id=prefix + "_start", unit="date"),
        candidate("2026-11-01", "2026-11-01", field_id=prefix + "_end", unit="date"),
    )
    value["document_type"] = kind
    checked = verify_response(value, ["2026-11-30\n2026-11-01"], kind)
    assert all(
        item.validation_status == "needs_review" and item.normalized_value is None for item in checked.fields
    )
    assert all(
        item.evidence_valid and "PERIOD_CONFLICT" in item.validation_reasons for item in checked.fields
    )


def test_foreign_document_fields_are_isolated_before_date_pair_validation():
    value = response(
        candidate("100", "100", field_id="gross", unit="KRW"),
        candidate("abc", "abc", field_id="contract_start", unit="text"),
        candidate("xyz", "xyz", field_id="contract_end", unit="text"),
    )
    value["document_type"] = "payslip"
    checked = verify_response(value, ["100\nabc\nxyz"], "payslip")
    assert checked.fields[0].validation_status == "valid"
    assert all(
        item.normalized_value is None and "UNKNOWN_FIELD" in item.validation_reasons
        for item in checked.fields[1:]
    )


def test_one_unreadable_field_preserves_independent_candidates_and_missing_values():
    good = candidate("3000000원", "3000000", field_id="contract_rate", unit="KRW/month")
    bad = candidate("twenty-fifth", "25")
    missing = candidate(None, None, field_id="contract_end", unit=None, quote="")
    checked = verify_response(response(good, bad, missing), ["3000000원\ntwenty-fifth"], "contract")
    assert [item.validation_status for item in checked.fields] == ["valid", "needs_review", "missing"]
    assert checked.fields[0].normalized_value == "3000000"
    assert checked.fields[1].normalized_value is None and checked.fields[1].suggested_value == "25"
    assert checked.fields[2].suggested_value is None


@pytest.mark.parametrize(
    "field", ["validation_status", "validation_reasons", "suggested_value", "evidence_valid"]
)
def test_model_cannot_assert_code_owned_validation_metadata(field):
    item = candidate()
    item[field] = "valid" if field == "validation_status" else []
    with pytest.raises(ModelFormatError):
        verify_response(response(item), ["25th"], "contract")


@pytest.mark.parametrize("kind", ["unknown", "mixed"])
def test_unresolved_document_classification_still_fails_the_job_contract(kind):
    value = response(candidate())
    value["document_type"] = kind
    with pytest.raises(ModelFormatError) as error:
        verify_response(value, ["25th"], "auto")
    assert error.value.reason == "DOCUMENT_TYPE_UNRESOLVED"


async def test_bad_quote_is_saved_as_review_text_without_fabricated_evidence_or_retry():
    data = pdf_bytes(["Payday: 25th"])
    ref = source(data)
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(
            ocr_result_id="test-ocr-1",
            source=ref,
            document_type="contract",
        ),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)

    class Provider:
        calls = 0

        async def invoke(self, operation, inputs):
            self.calls += 1
            return response(candidate("26th", "26", quote="Invented payday: 26th")), {"total_tokens": 17}

    provider = Provider()
    out = await run_task(task, ports=ports, settings=AgentSettings(), provider=provider)
    assert out.status == "completed", out.error
    assert provider.calls == 1 and out.usage == {"total_tokens": 17}
    item = out.payload.fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert item.suggested_value == "26" and item.raw_value == "26th"
    assert item.source_quote == "Invented payday: 26th" and item.source_page == 1
    assert item.evidence_refs == [] and out.payload.evidence_catalog == []
    assert ports.checkpoints[task.context.job_id].call_budget == {"extract_candidates": 1}
