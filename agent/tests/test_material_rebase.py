import copy

import pytest

from agent.config import AgentSettings
from agent.dev.comparison_materials import build_materials
from agent.dev.local_workflow import Confirmation, LocalStore
from agent.dev.material_rebase import material_candidate, rebase_review, same_selection
from agent.tests.test_comparison_materials import basic_store, selection
from agent.tests.test_linked_comparison import (
    SavedDocumentProvider,
    four_documents,
    stage,
)
from agent.tests.test_linked_comparison import (
    selection as linked_selection,
)


def prepared_materials():
    store = basic_store()
    for document in store.documents.values():
        document["pages"] = 2
    return build_materials(store, selection())


def review(materials):
    return material_candidate(Confirmation().model_dump(), materials)["material_review"]


def test_row_insertions_rebase_edits_by_source_label_page_and_not_index_id():
    before = prepared_materials()
    current = review(before)
    basic, allowance = before["earnings"]
    current["earnings"][basic["item_id"]] = "110"
    current["row_roles"][allowance["item_id"]] = "excluded"
    after = copy.deepcopy(before)
    # The old index-derived ID now belongs to an entirely different source item.
    newcomer = {**after["earnings"][0], "label": "새 수당", "amount": "5"}
    after["earnings"][0]["item_id"] = "basic-new-position"
    after["earnings"][1].update(item_id="allowance-new-position", amount="20")
    after["earnings"].insert(0, newcomer)
    rebased, warnings = rebase_review(before, after, current, review(after))
    assert rebased["earnings"][basic["item_id"]] == "5"
    assert rebased["earnings"]["basic-new-position"] == "110"
    assert rebased["earnings"]["allowance-new-position"] == "20"
    assert rebased["row_roles"]["allowance-new-position"] == "excluded"
    assert any("110" in message for message in warnings)


@pytest.mark.parametrize("damage", ["duplicate_label", "changed_page", "changed_source"])
def test_uncertain_statement_correspondence_keeps_new_candidate_and_warns(damage):
    before = prepared_materials()
    current = review(before)
    basic = before["earnings"][0]
    current["earnings"][basic["item_id"]] = "110"
    after = copy.deepcopy(before)
    if damage == "duplicate_label":
        after["earnings"].append({**after["earnings"][0], "item_id": "duplicate-basic"})
    elif damage == "changed_page":
        for item in after["evidence_catalog"]:
            if item["evidence_id"] in after["earnings"][0]["evidence_refs"]:
                item["page"] = 2
    else:
        next(s for s in after["sources"] if s["case_id"] == "slip")["ref"]["content_hash"] = "changed"
    rebased, warnings = rebase_review(before, after, current, review(after))
    assert rebased["earnings"][basic["item_id"]] == "100"
    assert any("이전 초안 이력" in message for message in warnings)


def test_transaction_reordering_preserves_allocation_only_for_unique_identity():
    before = prepared_materials()
    current = review(before)
    transaction = before["transactions"][0]
    current["transactions"][transaction["transaction_id"]].update(
        selected=True, amount="80", transaction_amount="95"
    )
    after = copy.deepcopy(before)
    after["transactions"][0]["transaction_id"] = "new-index"
    rebased, warnings = rebase_review(before, after, current, review(after))
    assert rebased["transactions"]["new-index"] == {
        "selected": True,
        "amount": "80",
        "transaction_amount": "95",
    }
    assert not warnings
    after["transactions"].append({**after["transactions"][0], "transaction_id": "ambiguous-index"})
    rebased, warnings = rebase_review(before, after, current, review(after))
    assert not rebased["transactions"]["new-index"]["selected"]
    assert rebased["transactions"]["new-index"]["amount"] == "90"
    assert any("이전 초안 이력" in message for message in warnings)


def test_explicit_bank_reference_preserves_identity_across_ocr_amount_change():
    before = prepared_materials()
    before["transactions"][0].update(account_number="123", reference="BANK-1")
    current = review(before)
    transaction = before["transactions"][0]
    current["transactions"][transaction["transaction_id"]].update(selected=True, amount="80")
    after = copy.deepcopy(before)
    after["transactions"][0].update(transaction_id="moved", amount="100", transaction_amount="100")
    rebased, _ = rebase_review(before, after, current, review(after))
    assert rebased["transactions"]["moved"] == {
        "selected": True,
        "amount": "80",
        "transaction_amount": "100",
    }


def test_manual_rows_keep_same_document_and_valid_page_only():
    before = prepared_materials()
    current = review(before)
    current["manual_rows"] = [
        {"row_id": "valid", "group": "earnings", "label": "누락 수당", "amount": "5", "page": 1},
        {"row_id": "out-of-range", "group": "earnings", "label": "기타", "amount": "5", "page": 2},
    ]
    after = copy.deepcopy(before)
    next(s for s in after["sources"] if s["case_id"] == "slip")["page_count"] = 1
    rebased, warnings = rebase_review(before, after, current, review(after))
    assert [row["row_id"] for row in rebased["manual_rows"]] == ["valid"]
    assert any("이전 초안 이력" in message for message in warnings)


def test_selection_list_order_does_not_discard_the_same_selected_documents():
    before = selection()
    before["deposit_case_ids"] = ["bank-1", "bank-2"]
    after = {**before, "deposit_case_ids": ["bank-2", "bank-1"]}
    assert same_selection(before, after)
    assert not same_selection(before, {**after, "payslip_case_id": "other-slip"})


async def test_reextract_then_reload_preserves_working_corrections_and_requires_new_confirmation(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    linked = await store.load_materials(cid, linked_selection(cases))
    draft = linked["draft"]
    draft.update(
        time_kind="actual", earnings_complete=True, deductions_complete=True, payment_scope_confirmed=True
    )
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    draft["manual"]["net"] = "99000"
    basic, allowance = linked["materials"]["earnings"]
    draft["material_review"]["earnings"][basic["item_id"]] = "95000"
    draft["material_review"]["row_roles"][allowance["item_id"]] = "excluded"
    draft["material_review"]["manual_rows"] = [
        {
            "row_id": "missed-bonus",
            "group": "earnings",
            "label": "누락 상여",
            "amount": "1000",
            "row_role": "detail",
            "page": 1,
        }
    ]
    transaction_id = linked["materials"]["transactions"][0]["transaction_id"]
    draft["material_review"]["transactions"][transaction_id].update(selected=True, amount="40000")
    await store.save_draft(cid, draft)
    await stage(store, cases["payslip"], "extract")
    assert (await store.get_case(cid))["materials_invalid"]
    refreshed = await store.load_materials(cid, linked_selection(cases))
    saved = refreshed["draft"]
    assert saved["manual"]["net"] == "99000"
    assert saved["material_review"]["earnings"][basic["item_id"]] == "95000"
    assert saved["material_review"]["row_roles"][allowance["item_id"]] == "excluded"
    assert saved["material_review"]["manual_rows"] == draft["material_review"]["manual_rows"]
    assert saved["material_review"]["transactions"][transaction_id]["amount"] == "40000"
    assert saved["material_review"]["transactions"][transaction_id]["selected"]
    assert not saved["earnings_complete"] and not saved["payment_scope_confirmed"]
    assert refreshed["confirmed"] is None and refreshed["confirmed_materials"] is None
    assert refreshed["confirmed_revision"] is None and refreshed["dirty"]
    assert refreshed["draft_history"][-1]["draft"] == draft
    with pytest.raises(ValueError):
        await store.create_job(cid, "analyze")
    await store.save_draft(cid, saved)
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    assert (await restored.get_case(cid))["draft"] == saved


async def test_changed_selection_archives_old_work_and_preserves_rows_for_same_payslip(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    linked = await store.load_materials(cid, linked_selection(cases))
    draft = linked["draft"]
    draft["manual"]["net"] = "123"
    draft["material_review"]["manual_rows"] = [
        {"row_id": "missing", "group": "earnings", "label": "수당", "amount": "5", "page": 1}
    ]
    await store.save_draft(cid, draft)
    previous = copy.deepcopy(store.cases[cid]["draft"])
    changed = await store.load_materials(cid, {**linked_selection(cases), "deposit_case_ids": []})
    assert changed["draft"]["manual"]["net"] == "100000"
    assert changed["draft"]["material_review"]["manual_rows"] == previous["material_review"]["manual_rows"]
    assert changed["draft_history"][-1]["reason"] == "before_material_selection_change"
    assert changed["draft_history"][-1]["draft"] == previous
