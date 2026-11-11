import copy

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import Confirmation, LocalStore, extraction_draft, merge_extraction_draft
from agent.schemas.public.results import ExtractionCandidate
from agent.tests.fixtures_support import pdf_bytes


def candidate(amount, *, document_id="test-document"):
    return ExtractionCandidate(
        document_id=document_id,
        document_version=1,
        document_type="contract",
        extraction_version="1.0",
        model_config_version="test",
        fields=[
            {
                "field_id": "contract_rate",
                "raw_value": amount,
                "normalized_value": amount,
                "unit": "KRW/hour",
                "evidence_refs": [],
            }
        ],
        evidence_catalog=[],
        issues=[],
        page_count=1,
        verification_method="text_match",
    )


@pytest.mark.parametrize("edited", ["17000", None, "0"])
def test_reextraction_keeps_user_edits_and_explicitly_cleared_values(edited):
    baseline = extraction_draft(candidate("15000"))
    draft = copy.deepcopy(baseline)
    draft["values"]["contract_rate"] = edited
    draft["manual"]["net"] = "3200000"
    merged, changes = merge_extraction_draft(draft, baseline, extraction_draft(candidate("12000")))
    assert merged["values"]["contract_rate"] == edited
    assert merged["manual"]["net"] == "3200000"
    assert changes == [
        {"group": "values", "field_id": "contract_rate", "preserved": edited, "candidate": "12000"}
    ]
    assert draft["values"]["contract_rate"] == edited


def test_reextraction_refreshes_only_unedited_unconfirmed_candidates():
    baseline = extraction_draft(candidate("15000"))
    merged, changes = merge_extraction_draft(baseline, baseline, extraction_draft(candidate("12000")))
    assert merged["values"]["contract_rate"] == "12000"
    assert not changes
    locked, changes = merge_extraction_draft(
        baseline, baseline, extraction_draft(candidate("12000")), baseline
    )
    assert locked["values"]["contract_rate"] == "15000"
    assert changes[0]["candidate"] == "12000"


@pytest.mark.parametrize(
    "group,rate,unit", [("values", "contract_rate", "contract_unit"), ("manual", "slip_rate", "slip_unit")]
)
def test_edited_rate_and_unit_are_preserved_as_a_pair(group, rate, unit):
    baseline = Confirmation().model_dump()
    baseline[group][rate], baseline[unit] = "12000", "KRW/hour"
    draft, incoming = copy.deepcopy(baseline), copy.deepcopy(baseline)
    draft[group][rate] = "17000"
    incoming[group][rate], incoming[unit] = "3000000", "KRW/month"
    merged, changes = merge_extraction_draft(draft, baseline, incoming)
    assert merged[group][rate] == "17000"
    assert merged[unit] == "KRW/hour"
    assert {item["field_id"] for item in changes} == {rate, unit}


async def test_reextraction_persists_previous_draft_and_confirmed_value(tmp_path):
    store = LocalStore(AgentSettings(), corpus=tmp_path / "corpus")
    case = await store.upload(pdf_bytes(["hourly_rate: 15000 KRW/hour"]))
    live = store.cases[case["id"]]
    store._apply(live, {"stage": "extract"}, candidate("15000", document_id=case["document_id"]))
    live["confirmed"] = copy.deepcopy(live["draft"])
    live["confirmed_revision"] = live["revision"]
    edited = copy.deepcopy(live["draft"])
    edited["values"]["contract_rate"] = "17000"
    await store.save_draft(case["id"], edited)
    store._apply(live, {"stage": "extract"}, candidate("12000", document_id=case["document_id"]))
    store.save_case(live)
    restored = LocalStore(AgentSettings(), corpus=tmp_path / "corpus")
    saved = await restored.get_case(case["id"])
    assert saved["draft"]["values"]["contract_rate"] == "17000"
    assert saved["confirmed"]["values"]["contract_rate"] == "15000"
    assert saved["draft_history"][-1]["draft"]["values"]["contract_rate"] == "17000"
    assert saved["extraction_changes"][0]["candidate"] == "12000"
    assert saved["dirty"]


async def test_partial_manual_row_draft_can_save_but_requires_a_label_to_confirm(tmp_path):
    from agent.dev.local_workflow import ComparisonInputError

    store = LocalStore(AgentSettings(), corpus=tmp_path / "corpus")
    case = await store.upload(pdf_bytes(["synthetic payslip"]), "payslip")
    live = store.cases[case["id"]]
    source_candidate = candidate("0", document_id=case["document_id"]).model_copy(
        update={"document_type": "payslip", "fields": []}
    )
    store._apply(live, {"stage": "extract"}, source_candidate)
    linked = await store.load_materials(live["id"], {"payslip_case_id": live["id"]})
    draft = copy.deepcopy(linked["draft"])
    draft.update(period_start="2026-10-01", period_end="2026-10-31")
    draft["material_review"]["manual_rows"] = [
        {
            "row_id": "added-1",
            "group": "earnings",
            "label": "",
            "amount": None,
            "row_role": "detail",
            "page": 1,
        }
    ]
    await store.save_draft(live["id"], draft)
    with pytest.raises(ComparisonInputError, match="항목명"):
        await store.confirm(live["id"])
    draft["material_review"]["manual_rows"][0].update(label="식대", amount="100000", page=2)
    with pytest.raises(ComparisonInputError, match="페이지"):
        await store.save_draft(live["id"], draft)


async def test_extraction_compatibility_is_independent_of_engine_without_losing_user_input(tmp_path):
    from agent.dev.local_workflow import ComparisonInputError

    corpus = tmp_path / "corpus"
    store = LocalStore(AgentSettings(), corpus=corpus)
    case = await store.upload(pdf_bytes(["hourly_rate: 15000 KRW/hour"]))
    live = store.cases[case["id"]]
    extraction = candidate("15000", document_id=case["document_id"])
    store._apply(live, {"stage": "extract"}, extraction)
    live["draft"]["values"]["contract_rate"] = "17000"
    live["draft"].update(period_start="2026-10-01", period_end="2026-10-31")
    live.pop("extraction_engine_version")
    store.save_case(live)
    restored = LocalStore(AgentSettings(), corpus=corpus)
    assert not (await restored.get_case(live["id"]))["extraction_needs_review"]
    assert (await restored.list_comparison_sources())[0]["has_extraction"]
    await restored.confirm(live["id"])
    restored.cases[live["id"]]["extraction_compatibility_version"] = "unsupported-extraction"
    restored.save_case(restored.cases[live["id"]])
    restored = LocalStore(AgentSettings(), corpus=corpus)
    assert (await restored.get_case(live["id"]))["extraction_needs_review"]
    assert not (await restored.list_comparison_sources())[0]["has_extraction"]
    with pytest.raises(ComparisonInputError, match="검증 기준"):
        await restored.confirm(live["id"])
    with pytest.raises(ComparisonInputError, match="검증 기준"):
        await restored.create_job(live["id"], "analyze")
    with pytest.raises(ComparisonInputError):
        await restored.load_materials(live["id"], {"contract_case_id": live["id"]})
    restored._apply(restored.cases[live["id"]], {"stage": "extract"}, extraction)
    assert not restored.cases[live["id"]]["extraction_needs_review"]
    assert restored.cases[live["id"]]["draft"]["values"]["contract_rate"] == "17000"
    assert (await restored.list_comparison_sources())[0]["has_extraction"]
