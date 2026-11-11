import io
import json
import zipfile
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from agent.config import AgentSettings
from agent.dev.payroll_pages import generate_inquiry, report_context, result_entry
from agent.schemas.public.base import Period
from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.results import (
    AnalysisResult,
    ComparisonResult,
    Question,
    WageComparisonItem,
    WageItemResult,
    WageSection,
)
from agent.schemas.public.tasks import AgentTask, ExplanationInput, ReportDisplayContext, ReportInput
from agent.schemas.public.wages import WageConditions
from agent.services.facts import allowed_facts, template_explanation
from agent.services.glossary import glossary_entries
from agent.services.hashing import content_hash, result_hash
from agent.services.inquiry import compose_inquiry
from agent.services.questions import build_questions, question_reason
from agent.services.reports import build_report, render_pdf, validate_display_context
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import analysis_fixture, context, pdf_bytes, source


def content_fixture():
    task, ref, data = analysis_fixture()
    snapshot = task.payload.snapshot
    data2 = pdf_bytes(["SYNTHETIC BANK RECORD"])
    ref2 = source(data2, source_id="source-bank")
    result = AnalysisResult(
        owner_id=snapshot.owner_id,
        case_id=snapshot.case_id,
        workplace_id=snapshot.workplace_id,
        input_version=1,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.snapshot_hash,
        context_id="context-content",
        context_hash="context-hash",
        rule_bundle_version="wage-v1",
        support_manifest_version="standard-v1",
        engine_version="agent-0.4.0",
        wage_section=WageSection(
            items=[
                WageItemResult(
                    rule_id="W-01",
                    status="calculated",
                    amount="2000000",
                    payable_amount="2000000",
                    formula_id="hourly-base",
                    operands={"ordinary_hourly_rate": "10000", "minutes": "12000"},
                    evidence_refs=["evidence-demo"],
                ),
                WageItemResult(
                    rule_id="W-02",
                    status="insufficient",
                    amount=None,
                    required_field_ids=["wage_conditions.schedule"],
                    reason_codes=["WEEK_CONTEXT_MISSING"],
                    required_periods=[Period(start="2026-09-28", end="2026-10-04")],
                ),
            ],
            subtotal="2000000",
            gross_total=None,
            scope_complete=False,
        ),
        comparisons=[
            ComparisonResult(
                rule_id="R-06",
                status="difference",
                lhs="1800000",
                rhs="1000000",
                delta="800000",
                formula_id="net-minus-allocation",
                scope_complete=True,
                evidence_refs=["evidence-demo"],
            )
        ],
        questions=[
            Question(
                question_id="q-schedule",
                reason_code="WEEK_CONTEXT_MISSING",
                target_field_ids=["wage_conditions.schedule"],
                blocking_rule_ids=["W-02"],
                text="9월 28일부터 10월 4일까지 근무 요일을 확인해 주세요.",
                reason="주휴 조건을 확인하기 위해 필요합니다.",
                required_materials=["근무표"],
                required_periods=[Period(start="2026-09-28", end="2026-10-04")],
                glossary_ids=["weekly_rest"],
            ),
            Question(
                question_id="q-other",
                reason_code="PAYMENT_SCOPE_UNCONFIRMED",
                target_field_ids=["transaction_allocations"],
                blocking_rule_ids=["R-06"],
                text="다른 질문은 선택하지 않았습니다.",
            ),
        ],
        evidence_catalog=snapshot.evidence_catalog,
        dependencies=[f"source:{ref.source_id}:1:1", f"source:{ref2.source_id}:1:1"],
        result_id="result-content",
        result_version=1,
        freshness="current",
        created_at=task.context.requested_at,
    )
    result.result_hash = result_hash(result)
    display = ReportDisplayContext(
        snapshot_id=result.snapshot_id,
        snapshot_hash=result.snapshot_hash,
        workplace_name="가상 테스트 사업장",
        period=snapshot.period,
        conditions=[
            {
                "label": "ordinary_hourly_rate",
                "value": "10,000원/시간",
                "state": "known",
                "evidence_refs": ["evidence-demo"],
            },
            {"label": "schedule", "value": "209", "state": "pending"},
        ],
        sources=[
            {"source_id": ref.source_id, "filename": "가상_급여명세서.pdf"},
            {"source_id": ref2.source_id, "filename": "가상_입금기록.pdf"},
        ],
    )
    return result, display, [(ref, data), (ref2, data2)]


def test_template_is_deterministic_and_keeps_missing_separate_from_zero():
    result, _, _ = content_fixture()
    before = result.model_dump_json()
    first = template_explanation(result)
    assert first == template_explanation(result)
    text = "\n".join(first.paragraphs)
    assert "주휴수당: 정보 부족" in text
    assert "주휴수당: 0원" not in text
    assert "800,000원" in text and "미지급이나 법 위반을 확정하지 않습니다" in text
    assert result.model_dump_json() == before
    assert ExplanationInput(result=result, allowed_facts=allowed_facts(result)).generation_mode == "template"


def test_questions_merge_condition_preserve_dates_and_affected_rules():
    task, _, _ = analysis_fixture()
    task.payload.snapshot.wage_conditions = WageConditions()
    items = [
        WageItemResult(
            rule_id=rule,
            status="insufficient",
            reason_codes=["WEEK_CONTEXT_MISSING"],
            required_field_ids=["wage_conditions.schedule"],
            required_periods=[dates],
        )
        for rule, dates in [
            ("W-02", Period(start="2026-09-28", end="2026-10-04")),
            ("W-05", Period(start="2026-10-26", end="2026-11-01")),
        ]
    ]
    questions = build_questions(items, [], snapshot=task.payload.snapshot)
    assert len(questions) == 1
    assert questions[0].blocking_rule_ids == ["W-02", "W-05"]
    assert len(questions[0].required_periods) == 2
    assert "기간 앞뒤" in questions[0].reason
    assert questions[0].required_materials and "scheduled_hours" in questions[0].glossary_ids


def test_comparison_only_user_has_no_unrelated_wage_questions():
    task, _, _ = analysis_fixture()
    snapshot = task.payload.snapshot
    snapshot.confirmed_fields = [
        f for f in snapshot.confirmed_fields if f.field_id in {"gross", "deduction", "net"}
    ]
    wage = WageItemResult(rule_id="W-01", status="insufficient", reason_codes=["WAGE_CONDITIONS_MISSING"])
    comparison = ComparisonResult(
        rule_id="R-06", status="insufficient", formula_id="r6", reason_codes=["PAYMENT_SCOPE_UNCONFIRMED"]
    )
    questions = build_questions([wage], [comparison], snapshot=snapshot)
    assert questions and all(q.blocking_rule_ids == ["R-06"] for q in questions)


def test_glossary_search_reviewed_sources_and_independent_copies():
    entries = glossary_entries()
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(entry["example"].startswith("예시(실제 사용자 금액 아님):") for entry in entries)
    ordinary = next(entry for entry in entries if entry["id"] == "ordinary_wage")
    assert any("law.go.kr" in item["url"] for item in ordinary["sources"])
    assert glossary_entries("공제", "급여")
    ordinary["sources"].clear()
    assert next(entry for entry in glossary_entries() if entry["id"] == "ordinary_wage")["sources"]


def test_inquiry_uses_selected_questions_and_rejects_foreign_question():
    result, display, _ = content_fixture()
    before = result.model_dump_json()
    draft = compose_inquiry(
        result, ["q-schedule", "q-schedule"], workplace_name=display.workplace_name, period=display.period
    )
    assert draft["selected_question_ids"] == ["q-schedule"]
    assert "9월 28일" in draft["text"] and "다른 질문은 선택하지 않았습니다" not in draft["text"]
    assert "800,000원" in draft["text"] and "미지급으로 단정" in draft["text"]
    assert draft["result_hash"] == result.result_hash and before == result.model_dump_json()
    with pytest.raises(ValueError, match="does not belong"):
        compose_inquiry(result, ["foreign-question"])


def test_report_display_context_is_frozen_and_bound_to_result():
    result, display, _ = content_fixture()
    with pytest.raises(ValidationError):
        display.period.start = display.period.end
    with pytest.raises(ValidationError):
        display.conditions[0].value = "invented"
    validate_display_context(result, display, "report-v2")
    with pytest.raises(IntegrityError, match="another snapshot"):
        validate_display_context(result, display.model_copy(update={"snapshot_hash": "stale"}), "report-v2")
    with pytest.raises(IntegrityError, match="requires"):
        validate_display_context(result, None, "report-v2")
    validate_display_context(result, None, "report-v1")


def test_pdf_contains_context_partial_scope_dates_filename_and_shared_glossary(tmp_path):
    result, display, _ = content_fixture()
    pdf = render_pdf(result, template_explanation(result), AgentSettings(), result.created_at, display)
    path = tmp_path / "content-report.pdf"
    path.write_bytes(pdf)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    for expected in [
        "가상 테스트 사업장",
        "2026-10-01",
        "2026-10-31",
        "통상시급",
        "미확정",
        "2026-09-28",
        "가상_급여명세서.pdf",
        "1쪽",
        "입금 배분액",
        "실제 사용자 금액 아님",
        "2,000,000원",
        "800,000원",
    ]:
        assert expected in text
    assert "209" not in text  # An unconfirmed condition value is never printed as a fact.


@pytest.mark.parametrize("selected_count", [0, 1, 2])
async def test_report_originals_none_subset_all_and_context_metadata(selected_count):
    result, display, originals = content_fixture()
    ports = FakePorts()
    ctx = context("build_report")
    ports.bind(ctx)
    for ref, data in originals:
        ports.add_source(ref, data)
    payload = ReportInput(
        result=result,
        explanation=template_explanation(result),
        explanation_version="template-v1",
        selected_sources=[ref for ref, _ in originals[:selected_count]],
        generated_at=ctx.requested_at,
        template_version="report-v2",
        display_context=display,
    )

    async def control():
        return None

    output = await build_report(
        SimpleNamespace(
            task=AgentTask(context=ctx, payload=payload),
            context=ctx,
            control=control,
            ports=ports,
            settings=AgentSettings(),
        )
    )
    assert len(output["included_source_ids"]) == selected_count
    archive = ports.artifacts[output["artifacts"][1]["artifact_id"]]
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert len(manifest) == selected_count
        assert all(content_hash(bundle.read(name)) == expected for name, expected in manifest.items())
        metadata = json.loads(bundle.read("report_context.json"))
        assert metadata["display_context"]["snapshot_hash"] == result.snapshot_hash
        dictionary = json.loads(bundle.read("glossary.json"))
        assert dictionary["entries"] == [
            entry
            for entry in glossary_entries()
            if entry["id"] in {item["id"] for item in dictionary["entries"]}
        ]


def test_monthly_included_allowance_does_not_claim_double_total():
    result, _, _ = content_fixture()
    item = result.wage_section.items[0]
    item.amount = Decimal("2000000")
    item.payable_amount = Decimal("1900000")
    text = "\n".join(template_explanation(result).paragraphs)
    assert "총액에 더한 금액: 1,900,000원" in text


def test_no_calculated_wages_does_not_present_placeholder_zero_as_subtotal():
    result, _, _ = content_fixture()
    result.wage_section.items = [result.wage_section.items[1]]
    result.wage_section.subtotal = Decimal("0")
    text = "\n".join(template_explanation(result).paragraphs)
    assert "아직 계산을 완료한 임금 항목이 없습니다" in text
    assert "소계: 0원" not in text


def test_report_context_freezes_confirmed_workplace_calendar_mapping_and_schedule():
    result, _, originals = content_fixture()
    task, _, _ = analysis_fixture()
    snapshot = task.payload.snapshot.model_dump(mode="json")
    snapshot["wage_conditions"] = {
        "schedule": {
            "state": "known",
            "value": {
                "days": [{"weekday": 0, "minutes": 480}],
                "week_start": 0,
                "weekly_rest_weekday": 6,
                "weekly_rest_minutes": 480,
            },
        },
        "five_or_more_workers": {"state": "known", "value": True},
        "days": [
            {"date": "2026-10-01", "kind": "paid_holiday", "attendance": "not_scheduled", "confirmed": True}
        ],
        "included_allowances": [{"rule_id": "W-03", "amount": "100000", "state": "known"}],
        "statement_mappings": [
            {
                "rule_ids": ["W-01"],
                "statement_item_ids": ["기본급"],
                "state": "known",
                "period": snapshot["period"],
            }
        ],
        "calculation_period": snapshot["period"],
    }
    case = {
        "title": "이 이름은 사업장이 아닌 비교 제목",
        "analysis": result.model_dump(mode="json"),
        "analysis_snapshot": snapshot,
        "analysis_period": snapshot["period"],
        "analysis_materials": {
            "sources": [{"case_id": "case-source", "identity": {"employer_name": "원문 회사"}}]
        },
        "analysis_confirmed": {"source_identity": {"case-source": {"employer_name": "확정 회사"}}},
    }
    store = SimpleNamespace(documents={ref.document_id: {"title": "가상 파일.pdf"} for ref, _ in originals})
    entry = result_entry(case)
    display = report_context(store, case, entry)
    assert display.workplace_name == "확정 회사"
    text = "\n".join(item.label + ": " + item.value for item in display.conditions)
    for value in [
        "월요일 480분",
        "주휴일 일요일",
        "유급휴일",
        "2026-10-01",
        "연장근로수당",
        "100,000원",
        "기본급",
        "예",
    ]:
        assert value in text
    assert "'weekday'" not in text and "True" not in text
    snapshot["wage_conditions"]["included_allowances"][0]["amount"] = "900000"
    assert "100,000원" in " ".join(item.value for item in display.conditions)
    draft = generate_inquiry(case, {"question_ids": []})
    assert "사업장: 확정 회사" in draft["text"] and case["title"] not in draft["text"]


def test_historical_report_context_and_hash_remain_bound_when_labelled_stale():
    result, display, _ = content_fixture()
    original = result.result_hash
    result.freshness = "stale"
    assert result_hash(result) == original
    validate_display_context(result, display, "report-v2")


def test_zero_total_difference_explains_component_cancellation_in_inquiry_and_pdf():
    result, display, _ = content_fixture()
    result.comparisons = [
        ComparisonResult(
            rule_id="R-07",
            status="difference",
            lhs="2000000",
            rhs="2000000",
            delta="0",
            formula_id="r7",
            scope_complete=True,
            reason_codes=["WAGE_COMPONENT_DIFFERENCE"],
            wage_items=[
                WageComparisonItem(
                    rule_ids=["W-01"],
                    statement_item_ids=["internal-statement-abcdef123456"],
                    status="difference",
                    lhs="1900000",
                    rhs="1800000",
                    delta="100000",
                )
            ],
        )
    ]
    manual = result.evidence_catalog[0].model_copy(
        update={
            "evidence_id": "manual-evidence",
            "source_id": "manual-internal-abcdef123456",
            "page": None,
            "verification_method": "manual_confirmed",
        }
    )
    result.evidence_catalog.append(manual)
    result.wage_section.items[0].evidence_refs = [manual.evidence_id]
    display = ReportDisplayContext(
        **{
            **display.model_dump(),
            "conditions": [
                {
                    "label": "계산·명세서 대응 범위: 기본급",
                    "value": "2026-10-01 ~ 2026-10-31 / 명세서 항목: internal-statement-abcdef123456",
                    "state": "known",
                    "evidence_refs": [manual.evidence_id],
                }
            ],
        }
    )
    explanation = template_explanation(result)
    inquiry = compose_inquiry(result, [])
    assert "총액 차이는 0원이지만 세부 항목별 차이가 서로 상쇄" in inquiry["text"]
    assert "계산 임금: 차이 발견" in inquiry["text"]
    pdf = render_pdf(result, explanation, AgentSettings(), result.created_at, display)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "사용자가 확인한 입력" in text and "선택한 명세서 항목 1개" in text
    assert "대응 항목 기본급" in text and "서로 상쇄" in text
    assert "manual-internal-abcdef123456" not in text and "internal-statement-abcdef123456" not in text
    assert "internal-statement-abcdef123456" in result.model_dump_json()
    assert "자정을 넘는" in question_reason("HOLIDAY_BOUNDARY_REVIEW")
