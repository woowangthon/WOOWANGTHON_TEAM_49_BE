import pytest

from agent.runner import failure_message
from agent.schemas.public.errors import ModelFormatError


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("DOCUMENT_TYPE_MISMATCH", "문서 유형을 변경"),
        ("DOCUMENT_TYPE_UNRESOLVED", "파일을 나누거나 유형을 직접 선택"),
        ("SOURCE_NORMALIZATION_INVALID", "수치·단위"),
        ("SOURCE_VALUE_MISMATCH", "값의 표기"),
        ("SOURCE_QUOTE_MISMATCH", "근거 문장"),
        ("TABLE_SOURCE_MISMATCH", "표의 제목·행"),
        ("TABLE_CELL_SOURCE_MISMATCH", "해당 OCR 행·열"),
        ("TABLE_ROW_SOURCE_REUSED", "중복 분류"),
        ("MODEL_FORMAT_INVALID", "형식 또는 원문 근거"),
    ],
)
def test_validation_failures_explain_category_and_action_without_source_text(reason, expected):
    message = failure_message(ModelFormatError("PRIVATE SOURCE VALUE", reason=reason))
    assert expected in message
    assert reason in message
    assert "주세요" in message
    assert "PRIVATE SOURCE VALUE" not in message


@pytest.mark.parametrize(
    "field_id,label",
    [
        ("break_minutes", "휴게 시간 (분)"),
        ("scheduled_daily_minutes", "일 소정근로시간 (분)"),
        ("transaction_1_deposit_amount", "입금액 (원)"),
    ],
)
def test_known_fields_have_reviewable_labels_and_ids(field_id, label):
    message = failure_message(
        ModelFormatError("PRIVATE SOURCE VALUE", reason="SOURCE_VALUE_MISMATCH", field_id=field_id)
    )
    assert f"{label} [{field_id}]" in message
    assert "PRIVATE SOURCE VALUE" not in message


def test_untrusted_reason_and_field_are_not_exposed():
    message = failure_message(
        ModelFormatError("PRIVATE EXCEPTION", reason="PRIVATE REASON", field_id="PRIVATE FIELD")
    )
    assert "MODEL_FORMAT_INVALID" in message
    assert "PRIVATE" not in message


def test_unexpected_exceptions_do_not_expose_internal_or_document_details():
    message = failure_message(RuntimeError("PRIVATE EXCEPTION"))
    assert message == "처리에 실패했습니다. 코드와 작업 ID로 확인해 주세요."
