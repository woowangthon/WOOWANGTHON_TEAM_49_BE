"""An individually checked candidate must remain safe to load into confirmation."""

from copy import deepcopy

import pytest

from agent.dev.local_workflow import Confirmation, extraction_draft
from agent.graphs.extraction_graph import verify_response


def checked_candidate(document_type, field_id, raw, suggested, unit, quote=None):
    quote = raw if quote is None else quote
    response = {
        "document_type": document_type,
        "fields": [
            {
                "field_id": field_id,
                "raw_value": raw,
                "normalized_value": suggested,
                "unit": unit,
                "page": 1,
                "quote": quote,
                "issues": [],
            }
        ],
        "issues": [],
    }
    original = deepcopy(response)
    reviewed = verify_response(response, [quote], document_type)
    assert response == original
    candidate = reviewed.fields[0]
    confirmation = Confirmation.model_validate(extraction_draft(reviewed.model_dump()))
    return candidate, confirmation


@pytest.mark.parametrize(
    "document_type,field_id,raw,suggested,unit,status",
    [
        pytest.param(
            "attendance",
            "worked_minutes",
            "0.5 minutes",
            "0.5",
            "minute",
            "needs_review",
            id="fractional-minute",
        ),
        pytest.param(
            "attendance",
            "worked_minutes",
            "0.5 hours",
            "30",
            "minute",
            "valid",
            id="fractional-hour-integer-minutes",
        ),
        pytest.param(
            "attendance", "worked_minutes", "0 minutes", "0", "minute", "valid", id="explicit-zero-minutes"
        ),
        pytest.param(
            "attendance",
            "worked_minutes",
            "10000000000000 minutes",
            "10000000000000",
            "minute",
            "needs_review",
            id="minutes-exceed-input-width",
        ),
        pytest.param(
            "contract", "contract_rate", "-100", "-100", "KRW/hour", "needs_review", id="negative-rate"
        ),
        pytest.param(
            "deposit", "deposit_total", "-100", "-100", "KRW", "needs_review", id="negative-deposit"
        ),
        pytest.param("payslip", "gross", "-100", "-100", "KRW", "valid", id="signed-payslip-adjustment"),
        pytest.param(
            "payslip", "gross", "100.001", "100.001", "KRW", "needs_review", id="money-exceeds-two-decimals"
        ),
        pytest.param("payslip", "gross", "100.01", "100.01", "KRW", "valid", id="money-two-decimals"),
        pytest.param(
            "payslip", "gross", "9999999999999.99", "9999999999999.99", "KRW", "valid", id="money-input-limit"
        ),
        pytest.param(
            "payslip",
            "gross",
            "10000000000000",
            "10000000000000",
            "KRW",
            "needs_review",
            id="money-exceeds-input-width",
        ),
        pytest.param(
            "contract",
            "contract_rate",
            "9999999999999.99",
            "9999999999999.99",
            "KRW/hour",
            "valid",
            id="rate-input-limit",
        ),
    ],
)
def test_range_review_does_not_break_confirmation_loading(
    document_type, field_id, raw, suggested, unit, status
):
    candidate, confirmation = checked_candidate(document_type, field_id, raw, suggested, unit)
    assert candidate.validation_status == status
    assert candidate.suggested_value == suggested
    assert candidate.raw_value == raw
    values = confirmation.values if document_type == "contract" else confirmation.manual
    input_key = "deposit" if field_id == "deposit_total" else field_id
    if status == "valid":
        assert candidate.normalized_value == suggested
        assert values[input_key] == suggested
    else:
        assert candidate.normalized_value is None
        assert candidate.validation_reasons
        assert values.get(input_key) is None


@pytest.mark.parametrize(
    "raw,unit,quote",
    [
        ("3000000", "KRW/hour", "월급 3000000원"),
        ("15000", "KRW/month", "시급 15000원"),
    ],
)
def test_explicit_rate_unit_conflict_remains_a_review_candidate(raw, unit, quote):
    candidate, confirmation = checked_candidate("contract", "contract_rate", raw, raw, unit, quote)
    assert candidate.validation_status == "needs_review"
    assert candidate.normalized_value is None
    assert candidate.suggested_value == raw
    assert candidate.validation_reasons
    assert confirmation.values.get("contract_rate") is None


def test_contract_input_length_limit_does_not_truncate_the_source_claim():
    raw = "정기 급여 지급일은 " + "해당 월의 " * 20 + "25일"
    assert len(raw) > 100
    candidate, confirmation = checked_candidate("contract", "payday", raw, "25", "day_of_month")
    assert candidate.validation_status == "valid"
    assert candidate.raw_value == raw
    assert candidate.source_quote == raw
    assert confirmation.values["payday"] == "25"
