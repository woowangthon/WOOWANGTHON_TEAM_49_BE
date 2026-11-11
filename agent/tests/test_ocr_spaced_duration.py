"""OCR spacing inside the Korean hour unit changes parsing, never source text."""

from copy import deepcopy

import pytest

from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import ModelFormatError
from agent.services.extraction_tables import source_cell
from agent.services.normalization import normalize_evidenced_scalar, normalize_scalar

QUOTE = "월 - 금 주 5 일 / 소 정 근 로 시 간 : 1 일 8 시 간 , 1 주 40 시 간"


def response(raw, quote=QUOTE, field_id="scheduled_daily_minutes", value="480"):
    return {
        "document_type": "contract",
        "fields": [
            {
                "field_id": field_id,
                "raw_value": raw,
                "normalized_value": value,
                "unit": "minute",
                "page": 1,
                "quote": quote,
                "issues": [],
            }
        ],
        "issues": [],
    }


@pytest.mark.parametrize("raw,value", [("8 시 간", "480"), ("1 시 간 30 분", "90"), ("0.5 시 간", "30")])
def test_hour_unit_syllable_spacing_is_only_internal_parser_tolerance(raw, value):
    assert normalize_scalar(raw, "minute") == value


@pytest.mark.parametrize(
    "raw,field_id,value,source_raw",
    [
        ("8 시 간", "scheduled_daily_minutes", "480", "8 시 간"),
        ("40 시 간", "scheduled_weekly_minutes", "2400", "40 시 간"),
        ("8시간", "scheduled_daily_minutes", "480", "8 시 간"),
        ("8", "scheduled_daily_minutes", "480", "8"),
    ],
)
def test_actual_failed_schedule_preserves_ocr_source(raw, field_id, value, source_raw):
    result = response(raw, field_id=field_id, value=value)
    original = deepcopy(result)
    verified = verify_response(result, [QUOTE], "contract")
    assert verified.fields[0].raw_value == source_raw
    assert verified.fields[0].normalized_value == value
    assert verified.fields[0].quote == QUOTE
    assert result == original


@pytest.mark.parametrize(
    "raw,quote",
    [
        ("8 시 간", "1 일 18 시 간"),
        ("8 시 간", "1 일 - 8 시 간"),
        ("8", "1 일 - 8 시 간"),
        ("8 시 간", "1 주 8 시 간"),
        ("8", "1 주 8 시 간"),
        ("8 시 간", "1 일 8 시 간 30 분"),
        ("8", "1 일 8 시 간 30 분"),
        ("8시간", "1일 8 hours"),
        ("8 hours", "1일 8 시 간"),
    ],
)
def test_spacing_tolerance_keeps_number_sign_period_component_and_unit_boundaries(raw, quote):
    item = verify_response(response(raw, quote), [quote], "contract").fields[0]
    assert item.validation_status == "needs_review"
    assert item.normalized_value is None and item.validation_reasons


def test_bare_table_cell_uses_explicit_spaced_hour_heading():
    quote = "기록 단위: 시 간(h)\n2026-10-01 8"
    assert normalize_evidenced_scalar("8", "minute", quote, field_id="work_1_worked_minutes") == "480"


def test_table_cell_preserves_spaced_source_unit_instead_of_inventing_a_header():
    row = "2026-10-01 8 시 간"
    assert source_cell("8시간", "480", "minute", "work_1_worked_minutes", row, row) == (
        "8 시 간",
        "480",
        ["SOURCE_SCALAR_SPELLING_RESTORED"],
    )


def test_table_cell_rejects_spaced_hour_unit_with_conflicting_minute_alias():
    row = "2026-10-01 8 시 간"
    with pytest.raises(ModelFormatError):
        source_cell("8 시 간(min)", "480", "minute", "work_1_worked_minutes", row, row)
