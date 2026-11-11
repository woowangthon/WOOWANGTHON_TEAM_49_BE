"""No external model calls or user-document extraction in this test suite."""

import subprocess

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import OCRFailed, OCRUnavailable
from agent.services import local_ocr
from agent.tests.fixtures_support import pdf_bytes
from agent.tests.support.payslip_ocr import extract_payslip


def page(*lines, confidence=95):
    return {"text": "\n".join(lines), "lines": [{"text": line, "confidence": confidence} for line in lines]}


def by_id(response):
    return {item["field_id"]: item for item in response["fields"]}


def test_side_by_side_amounts_dates_and_repeated_details():
    pages = [
        page(
            "대상기간: 2026-10-01 ~ 2026-10-31",
            "지급일: 2026년 11월 5일",
            "기본급 2,800,000 국민연금 135,000",
            "식대 100,000 건강보험 100,000",
            "식대 100,000 소득세 65,000",
            "총 지급액 3,000,000 공제 합계 300,000",
            "실 수령액 2,700,000",
        )
    ]
    response = extract_payslip(pages)
    values = by_id(response)
    assert values["gross"]["normalized_value"] == "3000000"
    assert values["deduction"]["normalized_value"] == "300000"
    assert values["net"]["normalized_value"] == "2700000"
    assert values["payment_date"]["normalized_value"] == "2026-11-05"
    assert values["period_start"]["normalized_value"] == "2026-10-01"
    assert (
        values["earning_2_amount"]["normalized_value"]
        == values["earning_3_amount"]["normalized_value"]
        == "100000"
    )
    assert values["deduction_1_amount"]["normalized_value"] == "135000"
    verify_response(response, [pages[0]["text"]], "payslip_ocr")


def test_missing_totals_are_never_summed_and_unclassified_text_retained():
    source = page("기본급 2,800,000", "식대 200,000", "특이한 표 양식", "사번 12345", "계좌 111222333")
    response = extract_payslip([source])
    values = by_id(response)
    assert values["gross"]["raw_value"] is None
    assert values["net"]["normalized_value"] is None
    assert len([key for key in values if key.endswith("_amount")]) == 2


def test_low_confidence_and_conflicting_totals_stay_unconfirmed():
    response = extract_payslip(
        [page("총지급액 3,000,000"), page("총지급액 4,000,000"), page("실수령액 2,700,000", confidence=30)]
    )
    values = by_id(response)
    assert values["gross"]["normalized_value"] is None
    assert "OCR_CONFLICTING_VALUES" in values["gross"]["issues"]
    assert values["net"]["raw_value"] == "2,700,000"
    assert values["net"]["normalized_value"] is None
    assert "OCR_LOW_CONFIDENCE" in values["net"]["issues"]


def test_unknown_layout_returns_manual_review_instead_of_model_fallback():
    response = extract_payslip([page("판독된 낯선 문서 형식")])
    assert "OCR_LAYOUT_REQUIRES_MANUAL_REVIEW" in response["issues"]
    assert all(item["normalized_value"] is None for item in response["fields"])


def test_rate_and_quantity_columns_are_not_misread_as_amounts():
    values = by_id(extract_payslip([page("기본급 12,000 x 160 = 1,920,000", "기본급 12,000 160 1,920,000")]))
    assert not any(key.endswith("_amount") for key in values)


def test_printed_subtotals_and_totals_are_preserved_for_row_role_review():
    from agent.services.extraction_profiles import statement_row_role

    source = page(
        "지급내역",
        "기본급 100,000",
        "식대 20,000",
        "소계 120,000",
        "총지급액 120,000",
        "공제내역",
        "소득세 10,000",
        "공제 소계 10,000",
        "공제합계 10,000",
    )
    response = extract_payslip([source])
    values = by_id(response)
    labels = [item["normalized_value"] for key, item in values.items() if key.endswith("_label")]
    assert labels == ["기본급", "식대", "소계", "총지급액", "소득세", "공제 소계", "공제합계"]
    assert [statement_row_role(label) for label in labels] == [
        "detail",
        "detail",
        "subtotal",
        "total",
        "detail",
        "subtotal",
        "total",
    ]
    assert values["gross"]["normalized_value"] == "120000"
    verify_response(response, [source["text"]], "payslip_ocr")


def test_tsv_groups_visual_rows_across_tesseract_blocks():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    rows = [
        "5\t1\t1\t1\t1\t1\t10\t10\t50\t20\t96\t기본급",
        "5\t1\t1\t1\t1\t2\t100\t11\t50\t20\t95\t3,000,000",
        "5\t1\t2\t1\t1\t1\t300\t10\t50\t20\t90\t국민연금",
        "5\t1\t2\t1\t2\t1\t10\t50\t50\t20\t93\t식대",
    ]
    lines = local_ocr.tsv_lines(header + "\n".join(rows))
    assert [line["text"] for line in lines] == ["기본급 3,000,000 국민연금", "식대"]
    assert lines[0]["confidence"] == 90


@pytest.mark.parametrize("scanned", [False, True])
async def test_legacy_alias_uses_ocr_then_ai_and_restores_type(tmp_path, monkeypatch, scanned):
    source = page("기본급 3,000,000", "총지급액 3,000,000", "공제합계 0", "실수령액 3,000,000")
    calls = []
    model_calls = []

    class ClassifyingProvider:
        async def invoke(self, operation, data):
            model_calls.append((operation, data))
            assert calls == [[0]], "AI classification must wait for OCR fallback"
            assert data["source_method"] == "python_ocr"
            assert data["document_type"] == "payslip"
            assert "pdf_bytes" not in data
            assert data["pages"] == [source["text"]]
            response = extract_payslip([source])
            response["document_type"] = "payslip"
            return response, {}

    def recognize(data, settings, indices):
        calls.append(indices)
        return [source]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    # Exercise the legacy alias on pages whose text layer is unusable; native
    # text is covered independently by the four-type text-first workflow tests.
    monkeypatch.setattr("agent.services.document_ocr.read_pdf_text_page", lambda page: None)
    settings = AgentSettings(provider_mode="openai")
    store = LocalStore(settings, state=tmp_path, provider=ClassifyingProvider())
    case = await store.upload(pdf_bytes(source["text"].splitlines(), scanned=scanned), "payslip")
    case = await store.change_document_type(case["id"], "payslip_ocr")
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    result = await store.get_case(case["id"])
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    assert calls == [[0]]  # An unusable text layer falls back to OCR.
    assert len(model_calls) == 1
    assert store.jobs[job["id"]]["call_budget"] == {"extract_candidates": 1}
    assert result["extraction"]["document_type"] == "payslip_ocr"
    assert result["extraction"]["recognized_pages"] == [source["text"]]
    assert result["extraction"]["verification_method"] == "ocr_unverified"
    assert all(e["verification_method"] == "ocr_unverified" for e in result["extraction"]["evidence_catalog"])
    assert result["draft"]["manual"]["gross"] == "3000000"
    assert not result["confirmed"]
    restored = LocalStore(settings, state=tmp_path, provider=ClassifyingProvider())
    assert (await restored.get_case(case["id"])) == result
    changed = await store.change_document_type(case["id"], "payslip")
    assert changed["extraction"] is None


@pytest.mark.parametrize(
    "failure,code",
    [
        (OCRUnavailable("OCR 엔진이 필요합니다."), "OCR_UNAVAILABLE"),
        (OCRFailed("OCR 처리 시간이 초과되었습니다."), "OCR_FAILED"),
    ],
)
async def test_ocr_error_is_actionable_and_never_model_invalid(tmp_path, monkeypatch, failure, code):
    def fail(*args):
        raise failure

    monkeypatch.setattr(local_ocr, "recognize_pdf", fail)
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.upload(pdf_bytes(["Synthetic scan"], scanned=True), "payslip_ocr")
    job = await store.create_job(case["id"], "ocr")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["error_code"] == code
    assert store.jobs[job["id"]]["error"] == str(failure)


def test_process_timeout_and_nonzero_exit_map_to_ocr_error(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ocr", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(OCRFailed, match="초과"):
        local_ocr.run(["engine"], 1)


@pytest.mark.real_ocr
def test_pdf_rasterization_and_stdin_ocr_contract(monkeypatch, tmp_path):
    binary = tmp_path / "engine.exe"
    binary.touch()
    languages = tmp_path / "한글 언어 폴더"
    languages.mkdir()
    calls = []

    def fake_run(command, timeout, *, cwd=None, data=None):
        calls.append((command, cwd, data))
        if "--list-langs" in command:
            return "Languages:\nkor\neng\n"
        assert data.startswith(b"\x89PNG")
        return "level\tleft\ttop\theight\tconf\ttext\n5\t10\t10\t20\t95\t기본급\n"

    monkeypatch.setattr(local_ocr, "run", fake_run)
    result = local_ocr.recognize_pdf(
        pdf_bytes(["Synthetic render"]), AgentSettings(ocr_command=str(binary), ocr_data_path=languages)
    )
    assert result[0]["text"] == "기본급"
    assert calls[1][0][1:3] == ["stdin", "stdout"]
    assert calls[1][1] == languages
    assert "--tessdata-dir" in calls[0][0] and "." in calls[0][0]
