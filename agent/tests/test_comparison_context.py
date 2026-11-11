"""Selection previews use source-backed metadata without inventing identity or totals."""

import copy

from agent.dev.comparison_context import comparison_compatibility, list_comparison_sources
from agent.dev.comparison_materials import build_materials, enrich_snapshot
from agent.dev.local_workflow import Confirmation, make_snapshot
from agent.graphs.extraction_graph import verify_response
from agent.tests.test_comparison_materials import basic_store, selection


def add_field(store, case_id, field_id, value, **extra):
    field = {
        "field_id": field_id,
        "raw_value": value,
        "normalized_value": value,
        "unit": "text",
        "validation_status": "valid",
        "evidence_refs": [],
        **extra,
    }
    store.cases[case_id]["extraction"]["fields"].append(field)
    return field


def test_source_cards_separate_ai_readiness_identity_and_review_status():
    store = basic_store()
    add_field(store, "contract", "employee_name", "나래")
    add_field(store, "work", "employee_name", "가온")
    add_field(store, "slip", "employee_name", "가온")
    add_field(store, "bank", "account_holder", "가온")
    add_field(store, "slip", "slip_actual_minutes", "9600", unit="minute")
    add_field(store, "slip", "slip_paid_minutes", "12480", unit="minute")
    add_field(
        store,
        "work",
        "overtime_minutes",
        None,
        suggested_value="100",
        validation_status="needs_review",
        unit="minute",
    )
    before = copy.deepcopy(store.cases)
    sources = {item["case_id"]: item for item in list_comparison_sources(store)}
    assert sources["work"]["review_needed_count"] == 1
    assert sources["work"]["has_extraction"]
    assert sources["work"]["summary"]["worked_time_kind"] == ""
    assert sources["bank"]["identity"]["employee_name"] == "가온"
    assert sources["slip"]["summary"]["slip_minutes"] == "9600"
    assert sources["slip"]["summary"]["slip_paid_minutes"] == "12480"
    assert sources["slip"]["summary"]["slip_time_kind"] == "actual"
    assert store.cases == before


def test_identity_conflict_cannot_be_accepted_by_checkbox_but_can_be_corrected():
    store = basic_store()
    add_field(store, "contract", "employee_name", "나래")
    add_field(store, "slip", "employee_name", "가온")
    materials = build_materials(store, selection())
    context = comparison_compatibility(materials, {"subjects_confirmed": True})
    assert context["blocked"]
    assert any(issue["code"] == "SUBJECT_MISMATCH" for issue in context["issues"])
    fixed = comparison_compatibility(
        materials,
        {
            "subjects_confirmed": True,
            "source_identity": {"contract": {"employee_name": "가온"}},
        },
    )
    assert not fixed["blocked"]
    assert materials["sources"][0]["identity"]["employee_name"] == "나래"


def test_missing_identity_is_never_inferred_from_filename_or_account():
    store = basic_store()
    store.documents["bank"]["title"] = "가온_월급테스트_급여입금.pdf"
    add_field(store, "bank", "account_number", "GAON-001")
    materials = build_materials(store, {"deposit_case_ids": ["bank"]})
    assert materials["sources"][0]["identity"]["employee_name"] is None
    context = comparison_compatibility(materials, {})
    assert context["requires_subject_confirmation"]
    assert not context["blocked"]
    assert {issue["code"] for issue in context["issues"]} >= {
        "SUBJECT_DETAILS_MISSING",
        "SUBJECT_CONFIRMATION_REQUIRED",
    }


def test_unit_and_time_basis_conflicts_do_not_block_independent_comparisons():
    materials = build_materials(basic_store(), selection())
    context = comparison_compatibility(
        materials,
        {
            "contract_unit": "KRW/month",
            "slip_unit": "KRW/hour",
            "worked_time_kind": "actual",
            "slip_time_kind": "paid",
            "subjects_confirmed": True,
        },
    )
    assert not context["blocked"]
    assert {issue["code"] for issue in context["issues"]} >= {"UNIT_MISMATCH", "TIME_MEANING_UNCONFIRMED"}


def test_bank_query_period_is_not_assumed_to_be_a_salary_period():
    store = basic_store()
    add_field(store, "bank", "period_start", "2026-11-01", unit="date")
    add_field(store, "bank", "period_end", "2026-11-30", unit="date")
    materials = build_materials(store, selection())
    context = comparison_compatibility(
        materials,
        {
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "subjects_confirmed": True,
        },
    )
    assert not any(issue["code"] == "PERIOD_MISMATCH" for issue in context["issues"])


def test_unusable_actual_time_does_not_fall_back_to_paid_or_legacy_total():
    store = basic_store()
    add_field(store, "slip", "slip_minutes", "12480", unit="minute")
    add_field(
        store,
        "slip",
        "slip_actual_minutes",
        None,
        suggested_value="9600",
        validation_status="needs_review",
        unit="minute",
    )
    add_field(store, "slip", "slip_paid_minutes", "12480", unit="minute")
    materials = build_materials(store, {"payslip_case_id": "slip"})
    assert materials["suggested"]["manual"]["slip_minutes"] is None
    assert materials["field_validation"]["slip_minutes"]["field_id"] == "slip_actual_minutes"
    assert materials["field_validation"]["slip_paid_minutes"]["normalized_value"] == "12480"


def test_common_identity_fields_remain_exact_source_candidates():
    response = {
        "document_type": "payslip",
        "issues": [],
        "fields": [
            {
                "field_id": "employee_name",
                "raw_value": "가상근로자 가온",
                "normalized_value": "가상근로자 가온",
                "unit": "text",
                "page": 1,
                "quote": "성명: 가상근로자 가온",
                "issues": [],
            }
        ],
    }
    checked = verify_response(response, ["성명: 가상근로자 가온"], "payslip")
    assert checked.fields[0].validation_status == "valid"
    response["fields"][0]["normalized_value"] = "나래"
    assert (
        verify_response(response, ["성명: 가상근로자 가온"], "payslip").fields[0].validation_status
        == "needs_review"
    )


def test_confirmed_payslip_unit_sets_profile_without_a_contract():
    materials = build_materials(basic_store(), {"payslip_case_id": "slip"})
    materials["suggested"]["slip_unit"] = "KRW/hour"
    confirmation = Confirmation(
        period_start="2026-10-01",
        period_end="2026-10-31",
        slip_unit="KRW/month",
        manual={"slip_rate": "3000000"},
    )
    snapshot, _, _ = make_snapshot({"id": "comparison-test", "confirmed_revision": 1}, None, confirmation)
    assert enrich_snapshot(snapshot, materials, {}).profile == "M-STD"
