"""Four authored PDF examples exercise independent saved-document comparisons."""

import copy

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import ComparisonInputError, LocalStore
from agent.schemas.public.results import ExtractionCandidate
from agent.services.hashing import content_hash
from agent.tests.support.comparison_documents import (
    ROOT,
    NoModelCalls,
    analyzed,
    author_gold,
    create_bundle,
    reviewed,
    seed_documents,
    selection,
)


async def setup(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    cases = await seed_documents(store)
    bundle = await create_bundle(store, cases)
    return store, cases, bundle["id"]


async def test_three_documents_are_a_comparison_not_a_document_and_match_author_gold(tmp_path):
    store, cases, cid = await setup(tmp_path)
    original = copy.deepcopy(store.cases)
    current = await reviewed(store, cid)
    assert current["kind"] == "comparison" and current["document_id"] is None
    assert cid not in store.cases and len(store.comparisons) == 1
    current, results = await analyzed(store, cid)
    expected = author_gold()["expected"]["without_contract"]
    assert {key: results[key]["status"] for key in expected} == expected
    assert results["R-07"]["status"] == "insufficient"
    assert results["R-02"]["lhs"] == results["R-02"]["rhs"] == "9600"
    assert results["R-06"]["lhs"] == results["R-06"]["rhs"] == "2888000"
    assert current["analysis_period"] == author_gold()["period"]
    assert current["analysis"]["wage_section"]["gross_total"] is None
    assert store.cases == original  # Comparison confirmation does not edit original candidates.
    assert {source["case_id"] for source in current["analysis_materials"]["sources"]} == {
        cases["attendance"],
        cases["payslip"],
        cases["deposit"],
    }


async def test_other_employee_monthly_contract_blocks_until_removed(tmp_path):
    store, cases, cid = await setup(tmp_path)
    current = await store.load_materials(cid, selection(cases, contract=True))
    codes = {issue["code"] for issue in current["compatibility"]["issues"]}
    assert set(author_gold()["expected"]["four_documents_issue_codes"]) <= codes
    assert current["compatibility"]["blocked"]
    with pytest.raises(ComparisonInputError, match="성명|사원번호|근로자"):
        await reviewed(store, cid)  # The same-person checkbox cannot override detected conflict.
    await store.load_materials(cid, selection(cases))
    await reviewed(store, cid)
    _, results = await analyzed(store, cid)
    assert results["R-01"]["status"] == "insufficient"
    assert results["R-06"]["status"] == "match"


async def test_one_deposit_difference_is_only_the_confirmed_linked_scope(tmp_path):
    store, _, cid = await setup(tmp_path)
    await reviewed(store, cid, selected_count=1, payment_scope=False)
    _, unconfirmed = await analyzed(store, cid)
    assert unconfirmed["R-06"]["status"] == "insufficient"
    await reviewed(store, cid, selected_count=1, payment_scope=True)
    current, results = await analyzed(store, cid)
    assert results["R-06"]["status"] == "difference"
    for key, value in author_gold()["expected"]["first_deposit_only"].items():
        assert results["R-06"][key] == value
    assert len(current["analysis_snapshot"]["transaction_allocations"]) == 1
    assert len(current["analysis_materials"]["transactions"]) == 2


async def test_actual_time_and_paid_basis_are_kept_separate(tmp_path):
    store, cases, cid = await setup(tmp_path)
    current = await store.get_comparison(cid)
    assert current["draft"]["manual"]["slip_minutes"] == "9600"
    sources = {item["case_id"]: item for item in await store.list_comparison_sources()}
    summary = sources[cases["payslip"]]["summary"]
    assert summary["slip_actual_minutes"] == "9600"
    assert summary["slip_paid_minutes"] == "12480"
    await reviewed(store, cid)
    draft = (await store.get_comparison(cid))["draft"]
    draft["manual"]["slip_minutes"] = "12480"
    draft["slip_time_kind"] = "paid"
    current = await store.save_draft(cid, draft)
    assert "TIME_MEANING_UNCONFIRMED" in {item["code"] for item in current["compatibility"]["issues"]}
    await store.confirm(cid)
    _, results = await analyzed(store, cid)
    assert results["R-02"]["status"] == "needs_review"
    assert results["R-02"]["delta"] is None
    assert results["R-05"]["status"] == results["R-06"]["status"] == "match"


async def test_unreviewed_net_leaves_other_comparisons_usable(tmp_path):
    store, cases, cid = await setup(tmp_path)
    case = store.cases[cases["payslip"]]
    net = next(field for field in case["extraction"]["fields"] if field["field_id"] == "net")
    net.update(
        normalized_value=None,
        validation_status="needs_review",
        validation_reasons=["SOURCE_VALUE_MISMATCH"],
        evidence_refs=[],
    )
    case["extraction_revision"] += 1
    store.save_case(case)
    current = await store.load_materials(cid, selection(cases))
    assert current["draft"]["manual"].get("net") is None
    assert current["materials"]["field_validation"]["net"]["suggested_value"] == "2888000"
    await reviewed(store, cid)
    _, results = await analyzed(store, cid)
    assert all(results[rule]["status"] == "match" for rule in ("R-02", "R-03", "R-04"))
    assert all(results[rule]["status"] == "insufficient" for rule in ("R-05", "R-06"))


async def test_comparison_changes_reload_and_source_refresh_are_isolated(tmp_path):
    store, cases, first_id = await setup(tmp_path)
    second_id = (await create_bundle(store, cases, title="선택 입금 비교"))["id"]
    await reviewed(store, first_id)
    first, _ = await analyzed(store, first_id)
    await reviewed(store, second_id, selected_count=1)
    second, results = await analyzed(store, second_id)
    assert results["R-06"]["delta"] == "888000"
    assert (await store.get_comparison(first_id))["analysis"] == first["analysis"]
    assert first["confirmed"] != second["confirmed"]
    reloaded = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    assert {item["id"] for item in await reloaded.list_comparisons()} == {first_id, second_id}
    assert (await reloaded.get_comparison(second_id))["analysis"] == second["analysis"]

    source = reloaded.cases[cases["payslip"]]
    payload = ExtractionCandidate.model_validate(source["extraction"])
    payload.fields = [
        field.model_copy(update={"normalized_value": "2887000", "suggested_value": "2887000"})
        if field.field_id == "net"
        else field
        for field in payload.fields
    ]
    reloaded._apply(source, {"stage": "extract"}, payload)
    reloaded.save_case(source)
    for cid in (first_id, second_id):
        stale = await reloaded.get_comparison(cid)
        assert stale["materials_invalid"] and stale["stale"]
        assert stale["analysis_history"][0]["snapshot"]["period"] == author_gold()["period"]
        with pytest.raises(ValueError):
            await reloaded.create_job(cid, "analyze")
    refreshed_input = await reloaded.load_materials(first_id, selection(cases))
    assert refreshed_input["draft"]["manual"]["net"] == "2888000"
    assert refreshed_input["materials"]["suggested"]["manual"]["net"] == "2887000"
    # A previously confirmed value is preserved until the user adopts the new candidate.
    draft = refreshed_input["draft"]
    draft["manual"]["net"] = "2887000"
    await reloaded.save_draft(first_id, draft)
    await reviewed(reloaded, first_id)
    refreshed, results = await analyzed(reloaded, first_id)
    assert results["R-06"]["delta"] == "-1000"
    assert refreshed["analysis_history"][0]["analysis"] == first["analysis"]
    assert (await reloaded.get_comparison(second_id))["materials_invalid"]


async def test_actual_four_pdf_bytes_keep_hashes_and_comparison_expectations(tmp_path):
    definitions = author_gold()["documents"].values()
    if not all((ROOT / item["path"]).is_file() for item in definitions):
        pytest.skip("Authored four-PDF corpus is not distributed with the source")
    before = {item["path"]: content_hash((ROOT / item["path"]).read_bytes()) for item in definitions}
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    cases = await seed_documents(store, actual_files=True)
    cid = (await create_bundle(store, cases))["id"]
    await reviewed(store, cid)
    _, results = await analyzed(store, cid)
    expected = author_gold()["expected"]["without_contract"]
    assert {key: results[key]["status"] for key in expected} == expected
    current = await store.load_materials(cid, selection(cases, contract=True))
    assert current["compatibility"]["blocked"]
    after = {item["path"]: content_hash((ROOT / item["path"]).read_bytes()) for item in definitions}
    assert before == after == {item["path"]: item["sha256"] for item in definitions}


async def test_original_identity_copied_by_form_cannot_hide_changed_source_subject(tmp_path):
    store, cases, cid = await setup(tmp_path)
    current = await store.get_comparison(cid)
    draft = current["draft"]
    # The browser sends every identity input, including unchanged source values.
    draft["source_identity"] = {
        source["case_id"]: {key: value or "" for key, value in source["identity"].items()}
        for source in current["materials"]["sources"]
    }
    await store.save_draft(cid, draft)
    await reviewed(store, cid)
    source = store.cases[cases["payslip"]]
    payload = ExtractionCandidate.model_validate(source["extraction"])
    for field in payload.fields:
        if field.field_id == "employee_name":
            field.raw_value = field.suggested_value = field.normalized_value = "가상근로자 나래"
    store._apply(source, {"stage": "extract"}, payload)
    store.save_case(source)
    refreshed = await store.load_materials(cid, selection(cases))
    assert refreshed["compatibility"]["blocked"]
    with pytest.raises(ComparisonInputError, match="성명|근로자"):
        await reviewed(store, cid)
