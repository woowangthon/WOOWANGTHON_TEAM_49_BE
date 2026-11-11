"""Mixed extraction results stay reviewable without entering confirmed calculations."""

import copy

import pytest

from agent.config import AgentSettings
from agent.dev.comparison_materials import build_materials, enrich_snapshot
from agent.dev.local_workflow import (
    Confirmation,
    LocalStore,
    extraction_draft,
    merge_extraction_draft,
)
from agent.dev.material_rebase import material_candidate, rebase_review
from agent.tests.fixtures_support import pdf_bytes
from agent.tests.test_comparison_materials import basic_store, selection, snapshot


def flag(store, case_id, field_id):
    field = next(f for f in store.cases[case_id]["extraction"]["fields"] if f["field_id"] == field_id)
    field.update(
        suggested_value=field["normalized_value"],
        validation_status="needs_review",
        validation_reasons=["SOURCE_NORMALIZATION_INVALID"],
        source_quote="AI-claimed source",
        source_page=1,
    )
    # Even inconsistent persisted candidates must not bypass the status gate.
    return field


def test_review_status_filters_draft_and_linked_candidates_but_keeps_diagnostics():
    store = basic_store()
    field = flag(store, "contract", "contract_rate")
    flag(store, "work", "worked_minutes")
    flag(store, "slip", "period_start")
    flag(store, "slip", "earning_1_amount")
    flag(store, "bank", "transaction_1_deposit_amount")
    draft = extraction_draft(store.cases["contract"]["extraction"])
    assert draft["values"]["contract_rate"] is None
    assert draft["values"]["contract_start"] == "2026-01-01"
    materials = build_materials(store, selection())
    assert "contract_rate" not in materials["suggested"]["values"]
    assert "worked_minutes" not in materials["suggested"]["manual"]
    assert materials["suggested"]["period_start"] is None
    metadata = materials["field_validation"]["contract_rate"]
    assert metadata["suggested_value"] == field["normalized_value"]
    assert metadata["normalized_value"] is None
    assert metadata["source_document_id"] == "contract"
    assert metadata["source_title"] == "contract.pdf"
    assert materials["earnings"][0]["amount"] is None
    assert materials["earnings"][0]["label"] == "기본급"
    assert materials["transactions"][0]["transaction_amount"] is None
    assert materials["transactions"][0]["validation_warnings"][0]["suggested_value"] == "90"


def test_amount_can_be_repaired_while_invalid_identity_requires_replacement():
    store = basic_store()
    flag(store, "slip", "earning_1_amount")
    materials = build_materials(store, selection())
    first, second = [row["item_id"] for row in materials["earnings"]]
    repaired = enrich_snapshot(snapshot(), materials, {"earnings": {first: "100", second: "20"}})
    assert repaired.earnings_complete
    assert [str(row.amount) for row in repaired.earnings] == ["100", "20"]
    flag(store, "slip", "earning_1_label")
    materials = build_materials(store, selection())
    unresolved = enrich_snapshot(snapshot(), materials, {"earnings": {first: "100", second: "20"}})
    assert not unresolved.earnings_complete
    assert unresolved.earnings[0].amount is None
    excluded = enrich_snapshot(
        snapshot(),
        materials,
        {"earnings": {second: "20"}, "row_roles": {first: "excluded"}},
    )
    assert excluded.earnings_complete  # Explicit exclusion permits the other reviewed rows.
    assert [str(row.amount) for row in excluded.earnings] == ["20"]


def test_invalid_transaction_identity_never_enters_allocations():
    store = basic_store()
    flag(store, "bank", "transaction_1_date")
    materials = build_materials(store, selection())
    tid = materials["transactions"][0]["transaction_id"]
    result = enrich_snapshot(
        snapshot(),
        materials,
        {"transactions": {tid: {"selected": True, "amount": "90", "transaction_amount": "90"}}},
    )
    assert result.transaction_allocations == []
    assert not result.payment_scope_confirmed


@pytest.mark.parametrize(
    "case_id,field_id,group",
    [
        ("slip", "earning_1_label", "earnings"),
        ("bank", "transaction_1_date", "transactions"),
    ],
)
def test_missing_required_row_identity_is_reviewed(case_id, field_id, group):
    store = basic_store()
    field = flag(store, case_id, field_id)
    field.update(
        raw_value=None,
        suggested_value=None,
        normalized_value=None,
        validation_status="missing",
        validation_reasons=[],
    )
    materials = build_materials(store, selection())
    warning = materials[group][0]["validation_warnings"][0]
    assert warning["validation_status"] == "missing"
    assert warning["validation_reasons"] == ["REQUIRED_VALUE_MISSING"]
    result = enrich_snapshot(snapshot(), materials, {})
    assert not (result.earnings_complete if group == "earnings" else result.payment_scope_confirmed)


def test_unused_bank_balance_issue_does_not_block_valid_deposit():
    store = basic_store()
    flag(store, "bank", "transaction_1_balance")
    materials = build_materials(store, selection())
    row = materials["transactions"][0]
    assert row["validation_warnings"] == []
    result = enrich_snapshot(
        snapshot(),
        materials,
        {
            "transactions": {
                row["transaction_id"]: {"selected": True, "amount": "90"},
            }
        },
    )
    assert result.payment_scope_confirmed
    assert str(result.transaction_allocations[0].amount) == "90"


def test_rebase_preserves_corrected_amount_but_deselects_invalid_identity():
    store = basic_store()
    fields = store.cases["bank"]["extraction"]["fields"]
    # An explicit reference keeps row identity stable across the date failure.
    fields.append({"field_id": "transaction_1_reference", "raw_value": "ref-1", "normalized_value": "ref-1"})
    before = build_materials(store, selection())
    tid = before["transactions"][0]["transaction_id"]
    current = material_candidate(Confirmation().model_dump(), before)["material_review"]
    current["transactions"][tid].update(selected=True, amount="50")
    flag(store, "bank", "transaction_1_date")
    after = build_materials(store, selection())
    candidate = material_candidate(Confirmation().model_dump(), after)["material_review"]
    rebased, warnings = rebase_review(before, after, current, candidate)
    assert rebased["transactions"][tid]["amount"] == "50"
    assert not rebased["transactions"][tid]["selected"]
    assert warnings


def test_review_refresh_preserves_user_corrections_without_adopting_suggestions():
    baseline = Confirmation(values={"contract_rate": "100"}).model_dump()
    current = copy.deepcopy(baseline)
    current["values"]["contract_rate"] = "150"
    candidate = copy.deepcopy(baseline)
    candidate["values"]["contract_rate"] = None
    merged, _ = merge_extraction_draft(current, baseline, candidate)
    assert merged["values"]["contract_rate"] == "150"
    untouched, _ = merge_extraction_draft(baseline, baseline, candidate)
    assert untouched["values"]["contract_rate"] is None


class MixedProvider:
    calls = 0

    async def invoke(self, operation, data):
        self.calls += 1
        return {
            "document_type": "contract",
            "issues": [],
            "fields": [
                {
                    "field_id": "contract_rate",
                    "raw_value": "100",
                    "normalized_value": "200",
                    "unit": "KRW/hour",
                    "page": 1,
                    "quote": "contract_rate: 100 KRW/hour",
                    "issues": [],
                },
                {
                    "field_id": "payday",
                    "raw_value": "25th",
                    "normalized_value": "25",
                    "unit": "day_of_month",
                    "page": 1,
                    "quote": "Payday: 25th of each month",
                    "issues": [],
                },
            ],
        }, {}


async def test_mixed_extraction_completes_once_reload_and_manual_confirmation(tmp_path):
    provider = MixedProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.upload(
        pdf_bytes(["contract_rate: 100 KRW/hour\nPayday: 25th of each month"]), "contract"
    )
    cid = case["id"]
    job = await store.create_job(cid, "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    assert provider.calls == 1
    result = await store.get_case(cid)
    assert result["draft"]["values"] == {"contract_rate": None, "payday": "25"}
    assert result["confirmed"] is None
    assert result["extraction"]["fields"][0]["suggested_value"] == "200"
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    saved = await restored.get_case(cid)
    assert saved["extraction"] == result["extraction"]
    draft = saved["draft"]
    draft.update(period_start="2026-10-01", period_end="2026-10-31")
    await restored.save_draft(cid, draft)
    deferred = await restored.confirm(cid)
    assert deferred["confirmed"]["values"]["contract_rate"] is None
    draft["values"]["contract_rate"] = "100"
    await restored.save_draft(cid, draft)
    confirmed = await restored.confirm(cid)
    assert confirmed["confirmed"]["values"]["contract_rate"] == "100"
    assert confirmed["extraction"]["fields"][0]["normalized_value"] is None
    assert provider.calls == 1
