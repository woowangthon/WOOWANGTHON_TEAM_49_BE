"""Comparison materials preserve original rows and require explicit confirmation."""

import copy
from types import SimpleNamespace

import pytest

from agent.dev.comparison_materials import (
    build_materials,
    enrich_snapshot,
    materials_current,
    reviewed_duplicate_groups,
)
from agent.schemas.public.inputs import (
    ConfirmedField,
    EvidenceRef,
    InputSnapshot,
    SourceRef,
    StatementItem,
    TransactionAllocation,
)
from agent.services.calculation_context import build_calculation_context
from agent.services.hashing import digest, snapshot_hash


def source(doc_id):
    return SourceRef(
        source_id="source-" + doc_id,
        owner_id="owner-local",
        workplace_id="workplace-local",
        link_id="link-" + doc_id,
        link_version=1,
        document_id=doc_id,
        document_version=1,
        content_hash=digest(doc_id),
    )


def store_with(*definitions):
    store = SimpleNamespace(cases={}, documents={}, document_ref=source)
    for case_id, kind, values in definitions:
        fields, evidence = [], []
        for field_id, value in values.items():
            unit = (
                "KRW" if any(word in field_id for word in ("amount", "gross", "net", "deduction")) else "text"
            )
            if isinstance(value, tuple):
                value, unit = value
            eid = digest([case_id, field_id])
            fields.append(
                {
                    "field_id": field_id,
                    "raw_value": value,
                    "normalized_value": value,
                    "unit": unit,
                    "evidence_refs": [eid] if value is not None else [],
                }
            )
            if value is not None:
                evidence.append(
                    EvidenceRef(
                        evidence_id=eid,
                        source_id="source-" + case_id,
                        page=1,
                        quote=field_id + ": " + value,
                        verification_method="text_match",
                    ).model_dump(mode="json")
                )
        store.documents[case_id] = {"sha256": digest(case_id), "title": case_id + ".pdf"}
        store.cases[case_id] = {
            "id": case_id,
            "document_id": case_id,
            "document_type": kind,
            "document_type_revision": 1,
            "extraction_revision": 1,
            "revision": 1,
            "extraction": {"document_type": kind, "fields": fields, "evidence_catalog": evidence},
        }
    return store


def snapshot():
    period = {"start": "2026-10-01", "end": "2026-10-31"}
    manual = source("manual").model_copy(update={"kind": "manual"})
    evidence = EvidenceRef(
        evidence_id="user-confirmation",
        source_id=manual.source_id,
        quote="사용자가 확인한 입력",
        verification_method="manual_confirmed",
    )
    result = InputSnapshot(
        snapshot_id="snapshot-test",
        input_version=1,
        owner_id="owner-local",
        workplace_id="workplace-local",
        case_id="case-test",
        period=period,
        confirmed_fields=[
            ConfirmedField(
                field_id=key,
                value=value,
                unit=unit,
                confirmation_id="confirm-" + key,
                evidence_refs=[evidence.evidence_id],
                period=period,
                time_kind="actual" if unit == "minute" else None,
            )
            for key, value, unit in (
                ("gross", "100", "KRW"),
                ("net", "90", "KRW"),
                ("slip_rate", "10", "KRW/hour"),
                ("worked_minutes", "480", "minute"),
            )
        ],
        authorized_sources=[manual],
        evidence_catalog=[evidence],
        earnings=[
            StatementItem(item_id="manual-earning", amount="100", evidence_refs=[evidence.evidence_id])
        ],
        deductions=[
            StatementItem(item_id="manual-deduction", amount="10", evidence_refs=[evidence.evidence_id])
        ],
        earnings_complete=True,
        deductions_complete=True,
        payment_scope_confirmed=True,
        transaction_allocations=[
            TransactionAllocation(
                transaction_id="manual-deposit",
                amount="90",
                transaction_amount="90",
                evidence_refs=[evidence.evidence_id],
            )
        ],
    )
    result.snapshot_hash = snapshot_hash(result)
    return result


def basic_store():
    return store_with(
        ("contract", "contract", {"contract_rate": ("10", "KRW/hour"), "contract_start": "2026-01-01"}),
        (
            "work",
            "attendance",
            {"period_start": "2026-10-01", "period_end": "2026-10-31", "worked_minutes": ("480", "minute")},
        ),
        (
            "slip",
            "payslip_ocr",
            {
                "period_start": "2026-10-01",
                "period_end": "2026-10-31",
                "gross": "100",
                "net": "90",
                "slip_rate": ("10", "KRW/hour"),
                "earning_1_label": "기본급",
                "earning_1_amount": "100",
                "earning_2_label": "미인식 수당",
                "earning_2_amount": None,
                "deduction_1_label": "세금",
                "deduction_1_amount": "10",
            },
        ),
        (
            "bank",
            "deposit",
            {
                "deposit_total": "90",
                "transaction_1_date": "2026-10-25",
                "transaction_1_sender": "회사",
                "transaction_1_deposit_amount": "90",
                "transaction_1_withdrawal_amount": "300",
                "transaction_1_balance": "9000",
                "transaction_2_date": "2026-10-25",
                "transaction_2_deposit_amount": None,
                "transaction_2_balance": "5000",
            },
        ),
    )


def selection():
    return {
        "contract_case_id": "contract",
        "attendance_case_ids": ["work"],
        "payslip_case_id": "slip",
        "deposit_case_ids": ["bank"],
    }


def test_build_keeps_real_rows_nulls_and_only_explicit_summaries():
    store = basic_store()
    before = copy.deepcopy(store.cases)
    materials = build_materials(store, selection())
    assert materials["suggested"] == {
        "values": {"contract_rate": "10", "contract_start": "2026-01-01"},
        "manual": {"worked_minutes": "480", "gross": "100", "net": "90", "slip_rate": "10"},
        "contract_unit": "KRW/hour",
        "slip_unit": "KRW/hour",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
    }
    assert [row["amount"] for row in materials["earnings"]] == ["100", None]
    assert [row["amount"] for row in materials["deductions"]] == ["10"]
    assert [row["transaction_amount"] for row in materials["transactions"]] == ["90", None]
    assert "deposit" not in materials["suggested"]["manual"]
    assert "deduction" not in materials["suggested"]["manual"]
    assert len({r["item_id"] for r in materials["earnings"]}) == 2
    assert materials["source_versions"]["slip"]["extraction_hash"] == digest(
        store.cases["slip"]["extraction"]
    )
    assert store.cases == before


def test_selection_requires_extracted_type_and_distinct_file_hashes():
    store = basic_store()
    with pytest.raises(ValueError, match="하나 이상"):
        build_materials(store, {})
    with pytest.raises(ValueError, match="문서 유형"):
        build_materials(store, {"payslip_case_id": "work"})
    store.cases["work"]["extraction"] = None
    with pytest.raises(ValueError, match="먼저"):
        build_materials(store, {"attendance_case_ids": ["work"]})
    store.documents["bank"]["sha256"] = store.documents["slip"]["sha256"]
    with pytest.raises(ValueError, match="동일한 파일"):
        build_materials(store, {"payslip_case_id": "slip", "deposit_case_ids": ["bank"]})


@pytest.mark.parametrize(
    "mutation", ["type", "type_revision", "extraction_revision", "extraction", "hash", "deleted"]
)
def test_source_versions_track_extraction_not_unrelated_draft_edit(mutation):
    store = basic_store()
    materials = build_materials(store, {"payslip_case_id": "slip"})
    store.cases["slip"]["revision"] += 1
    assert materials_current(store, materials)
    if mutation == "type":
        store.cases["slip"]["document_type"] = "payslip"
    elif mutation == "type_revision":
        store.cases["slip"]["document_type_revision"] += 1
    elif mutation == "extraction_revision":
        store.cases["slip"]["extraction_revision"] += 1
    elif mutation == "extraction":
        store.cases["slip"]["extraction"]["fields"][0]["normalized_value"] = None
    elif mutation == "hash":
        store.documents["slip"]["sha256"] = digest("replacement")
    else:
        del store.cases["slip"]
    assert not materials_current(store, materials)


def test_reviewed_rows_replace_manual_rows_and_keep_manual_and_source_evidence():
    materials = build_materials(basic_store(), selection())
    original = snapshot()
    row_id = materials["earnings"][1]["item_id"]
    tx_id = materials["transactions"][0]["transaction_id"]
    result = enrich_snapshot(
        original,
        materials,
        {"earnings": {row_id: "20"}, "transactions": {tx_id: {"selected": True, "amount": "60"}}},
    )
    assert [str(row.amount) for row in result.earnings] == ["100", "20"]
    assert result.earnings_complete and result.deductions_complete and result.payment_scope_confirmed
    assert len(result.transaction_allocations) == 1
    assert result.transaction_allocations[0].amount == 60
    assert result.transaction_allocations[0].transaction_amount == 90
    gross = next(field for field in result.confirmed_fields if field.field_id == "gross")
    assert gross.evidence_refs == ["user-confirmation", *materials["field_evidence"]["gross"]]
    assert len(result.authorized_sources) == 5
    assert len({e.evidence_id for e in result.evidence_catalog}) == len(result.evidence_catalog)
    assert result.snapshot_hash == snapshot_hash(result)
    assert result.snapshot_hash != original.snapshot_hash
    assert original.earnings[0].item_id == "manual-earning"
    assert len(enrich_snapshot(result, materials, {}).authorized_sources) == 5


def test_missing_rows_and_amounts_never_claim_complete_scope():
    materials = build_materials(basic_store(), {"payslip_case_id": "slip", "deposit_case_ids": ["bank"]})
    unresolved_id = materials["transactions"][1]["transaction_id"]
    result = enrich_snapshot(snapshot(), materials, {"transactions": {unresolved_id: {"selected": True}}})
    assert not result.earnings_complete
    assert not result.payment_scope_confirmed
    assert result.transaction_allocations == []
    materials["deductions"] = []
    assert not enrich_snapshot(snapshot(), materials, {}).deductions_complete


def test_wholly_unreadable_statement_rows_still_allow_other_comparisons():
    store = store_with(("slip", "payslip", {"earning_1_label": None, "earning_1_amount": None}))
    materials = build_materials(store, {"payslip_case_id": "slip"})
    result = enrich_snapshot(snapshot(), materials, {})
    assert result.earnings[0].amount is None and not result.earnings[0].confirmed
    assert not result.earnings_complete
    build_calculation_context(result, result.owner_id, result.case_id)
    corrected = enrich_snapshot(
        snapshot(), materials, {"earnings": {materials["earnings"][0]["item_id"]: "50"}}
    )
    assert corrected.earnings[0].evidence_refs == ["user-confirmation"]
    build_calculation_context(corrected, corrected.owner_id, corrected.case_id)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1,000", "1e3", 1, True, "0.001"])
def test_bad_review_amounts_rejected(bad):
    materials = build_materials(basic_store(), {"payslip_case_id": "slip"})
    item_id = materials["earnings"][0]["item_id"]
    with pytest.raises(ValueError):
        enrich_snapshot(snapshot(), materials, {"earnings": {item_id: bad}})


def test_overallocation_and_unrecognized_review_ids_rejected():
    materials = build_materials(basic_store(), {"deposit_case_ids": ["bank"]})
    tid = materials["transactions"][0]["transaction_id"]
    with pytest.raises(ValueError, match="초과"):
        enrich_snapshot(snapshot(), materials, {"transactions": {tid: {"selected": True, "amount": "91"}}})
    with pytest.raises(ValueError, match="존재하지"):
        enrich_snapshot(snapshot(), materials, {"transactions": {"invented": {"selected": True}}})
    with pytest.raises(ValueError):
        enrich_snapshot(snapshot(), materials, {"transactions": {tid: {"selected": "true"}}})


def test_no_selected_statement_or_bank_preserves_existing_manual_items():
    materials = build_materials(basic_store(), {"contract_case_id": "contract"})
    result = enrich_snapshot(snapshot(), materials, {})
    assert result.earnings[0].item_id == "manual-earning"
    assert result.deductions[0].item_id == "manual-deduction"
    assert result.transaction_allocations[0].transaction_id == "manual-deposit"


def test_subtotal_total_and_excluded_rows_do_not_double_count_or_block_complete_details():
    from agent.rules.comparisons import statements

    store = store_with(
        (
            "slip",
            "payslip",
            {
                "earning_1_label": "기본급",
                "earning_1_amount": "80",
                "earning_2_label": "식대",
                "earning_2_amount": "20",
                "earning_3_label": "소계",
                "earning_3_amount": "100",
                "earning_4_label": "지급총액",
                "earning_4_amount": None,
                "earning_5_label": "인식 오류",
                "earning_5_amount": None,
            },
        )
    )
    materials = build_materials(store, {"payslip_case_id": "slip"})
    assert [r["row_role"] for r in materials["earnings"]] == [
        "detail",
        "detail",
        "subtotal",
        "total",
        "detail",
    ]
    excluded_id = materials["earnings"][-1]["item_id"]
    before = copy.deepcopy(materials)
    result = enrich_snapshot(snapshot(), materials, {"row_roles": {excluded_id: "excluded"}})
    assert result.earnings_complete
    assert [r.is_total for r in result.earnings] == [False, False, True, True]
    comparison = statements(build_calculation_context(result, result.owner_id, result.case_id))[0]
    assert comparison.status == "match" and comparison.lhs == 100
    assert materials == before
    assert set(materials["earnings"][-1]["evidence_refs"]) <= {
        item.evidence_id for item in result.evidence_catalog
    }


def test_user_can_correct_inferred_rollup_role_without_losing_provenance():
    store = store_with(("slip", "payslip", {"earning_1_label": "소계", "earning_1_amount": "100"}))
    materials = build_materials(store, {"payslip_case_id": "slip"})
    item_id = materials["earnings"][0]["item_id"]
    assert not enrich_snapshot(snapshot(), materials, {}).earnings_complete
    result = enrich_snapshot(snapshot(), materials, {"row_roles": {item_id: "detail"}})
    assert result.earnings_complete and not result.earnings[0].is_total
    assert "user-confirmation" in result.earnings[0].evidence_refs
    for review in ({"row_roles": {"invented": "detail"}}, {"row_roles": {item_id: "invalid"}}):
        with pytest.raises(ValueError):
            enrich_snapshot(snapshot(), materials, review)


def test_manual_missing_rows_use_stable_ids_manual_evidence_and_original_page():
    store = store_with(("slip", "payslip", {"gross": "100"}))
    store.documents["slip"]["pages"] = 2
    materials = build_materials(store, {"payslip_case_id": "slip"})
    review = {
        "manual_rows": [
            {
                "row_id": "added-1",
                "group": "earnings",
                "label": "기본급",
                "amount": "100",
                "row_role": "detail",
                "page": 2,
            }
        ]
    }
    result = enrich_snapshot(snapshot(), materials, review)
    assert result.earnings_complete and result.earnings[0].item_id == "manual-added-1"
    evidence = next(e for e in result.evidence_catalog if e.locator == "manual-row:added-1")
    assert evidence.source_id == "source-slip" and evidence.page == 2
    assert evidence.verification_method == "manual_confirmed" and evidence.quote.startswith("사용자 보완:")
    assert evidence.evidence_id in result.earnings[0].evidence_refs
    assert "user-confirmation" in result.earnings[0].evidence_refs
    build_calculation_context(result, result.owner_id, result.case_id)
    assert enrich_snapshot(snapshot(), materials, review).snapshot_hash == result.snapshot_hash
    review["manual_rows"][0]["row_role"] = "excluded"
    excluded = enrich_snapshot(snapshot(), materials, review)
    assert not excluded.earnings and not excluded.earnings_complete
    assert any(e.locator == "manual-row:added-1" for e in excluded.evidence_catalog)


@pytest.mark.parametrize(
    "change",
    [
        {"page": 3},
        {"page": True},
        {"group": "other"},
        {"label": "  "},
        {"row_id": "../unsafe"},
        {"amount": 0},
        {"amount": "1e3"},
        {"row_role": "guess"},
    ],
)
def test_invalid_manual_statement_rows_are_rejected(change):
    store = store_with(("slip", "payslip", {"gross": "100"}))
    store.documents["slip"]["pages"] = 2
    materials = build_materials(store, {"payslip_case_id": "slip"})
    row = {"row_id": "a", "group": "earnings", "label": "기본급", "amount": "100", "page": 1}
    with pytest.raises(ValueError):
        enrich_snapshot(snapshot(), materials, {"manual_rows": [{**row, **change}]})
    with pytest.raises(ValueError):
        enrich_snapshot(snapshot(), materials, {"manual_rows": [row, row]})


def test_equal_dates_and_amounts_are_not_deduplicated():
    values = {
        "transaction_1_date": "2026-10-25",
        "transaction_1_deposit_amount": "90",
        "transaction_2_date": "2026-10-25",
        "transaction_2_deposit_amount": "90",
    }
    store = store_with(("bank", "deposit", values))
    rows = build_materials(store, {"deposit_case_ids": ["bank"]})["transactions"]
    assert len(rows) == 2 and rows[0]["transaction_id"] != rows[1]["transaction_id"]


def test_bank_reference_with_account_combines_one_logical_transaction_and_all_source_evidence():
    values = {
        "account_number": "123-456",
        "transaction_1_reference": "TX-001",
        "transaction_1_deposit_amount": "90",
    }
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    assert len(materials["transactions"]) == 1
    row = materials["transactions"][0]
    assert row["source_case_ids"] == ["bank1", "bank2"]
    assert len(row["evidence_refs"]) == 4
    assert row["duplicate_candidate_group"] is None
    assert any("식별자가 반복" in warning for warning in materials["warnings"])


@pytest.mark.parametrize("with_party", [True, False])
def test_similar_transfers_preserved_but_two_selected_require_distinct_confirmation(with_party):
    values = {"transaction_1_date": "2026-10-25", "transaction_1_deposit_amount": "90"}
    if with_party:
        values["transaction_1_sender"] = "회사"
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    rows = materials["transactions"]
    assert len(rows) == 2 and rows[0]["transaction_id"] != rows[1]["transaction_id"]
    assert rows[0]["duplicate_candidate_group"] == rows[1]["duplicate_candidate_group"]
    assert rows[0]["duplicate_candidate_group"].startswith("candidate-")
    changes = {row["transaction_id"]: {"selected": True} for row in rows}
    review = {"transactions": changes}
    result = enrich_snapshot(snapshot(), materials, review)
    assert len(result.transaction_allocations) == 2 and not result.payment_scope_confirmed
    review["distinct_transactions_confirmed"] = True
    assert enrich_snapshot(snapshot(), materials, review).payment_scope_confirmed
    changes[rows[1]["transaction_id"]]["selected"] = False
    review["distinct_transactions_confirmed"] = False
    assert enrich_snapshot(snapshot(), materials, review).payment_scope_confirmed
    review["distinct_transactions_confirmed"] = "true"
    with pytest.raises(ValueError):
        enrich_snapshot(snapshot(), materials, review)


def test_different_explicit_bank_references_distinguish_equal_transfers():
    values = {
        "account_number": "123-456",
        "transaction_1_date": "2026-10-25",
        "transaction_1_deposit_amount": "90",
        "transaction_1_reference": "TX-001",
    }
    values2 = {**values, "transaction_1_reference": "TX-002"}
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values2))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    rows = materials["transactions"]
    assert len(rows) == 2 and all(row["duplicate_candidate_group"] is None for row in rows)
    result = enrich_snapshot(
        snapshot(), materials, {"transactions": {row["transaction_id"]: {"selected": True} for row in rows}}
    )
    assert result.payment_scope_confirmed


@pytest.mark.parametrize(
    "left_extra,right_extra,expected_candidate",
    [
        ({"account_number": "123-456", "transaction_1_sender": "회사"}, {}, True),
        ({}, {"account_number": "123-456", "transaction_1_sender": "회사"}, True),
        ({"account_number": "123-456", "transaction_1_sender": "회사"}, {"account_number": "123-456"}, True),
        (
            {"account_number": "123-456", "transaction_1_sender": "급여회사"},
            {"account_number": "123-456", "transaction_1_sender": "주식회사 급여"},
            True,
        ),
        ({"transaction_1_description": "급여"}, {"transaction_1_description": "타행 입금"}, True),
        ({"account_number": "123-456"}, {"account_number": "999-888"}, False),
    ],
)
def test_candidate_detection_tolerates_missing_account_or_party_text_and_different_labels(
    left_extra, right_extra, expected_candidate
):
    values = {"transaction_1_date": "2026-10-25", "transaction_1_deposit_amount": "90"}
    store = store_with(
        ("bank1", "deposit", {**values, **left_extra}), ("bank2", "deposit", {**values, **right_extra})
    )
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    rows = materials["transactions"]
    assert len(rows) == 2
    if expected_candidate:
        assert (
            rows[0]["duplicate_candidate_group"]
            and rows[0]["duplicate_candidate_group"] == rows[1]["duplicate_candidate_group"]
        )
        reversed_rows = build_materials(store, {"deposit_case_ids": ["bank2", "bank1"]})["transactions"]
        assert rows[0]["duplicate_candidate_group"] == reversed_rows[0]["duplicate_candidate_group"]
        result = enrich_snapshot(
            snapshot(),
            materials,
            {"transactions": {row["transaction_id"]: {"selected": True} for row in rows}},
        )
        assert not result.payment_scope_confirmed
    else:
        assert all(row["duplicate_candidate_group"] is None for row in rows)


def test_conflicting_explicit_same_transaction_is_unresolved_until_original_amount_reviewed():
    values = {
        "account_number": "123-456",
        "transaction_1_date": "2026-10-25",
        "transaction_1_deposit_amount": "90",
        "transaction_1_reference": "TX-001",
    }
    values2 = {**values, "transaction_1_deposit_amount": "900"}
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values2))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    assert len(materials["transactions"]) == 1
    row = materials["transactions"][0]
    assert row["amount"] is None and row["transaction_amount"] is None
    assert any("원문마다 다릅니다" in warning for warning in materials["warnings"])
    review = {"transactions": {row["transaction_id"]: {"selected": True, "amount": "80"}}}
    assert not enrich_snapshot(snapshot(), materials, review).payment_scope_confirmed
    review["transactions"][row["transaction_id"]]["transaction_amount"] = "90"
    result = enrich_snapshot(snapshot(), materials, review)
    assert result.payment_scope_confirmed
    assert result.transaction_allocations[0].transaction_amount == 90
    assert "user-confirmation" in result.transaction_allocations[0].evidence_refs
    review["transactions"][row["transaction_id"]]["transaction_amount"] = "70"
    with pytest.raises(ValueError, match="초과"):
        enrich_snapshot(snapshot(), materials, review)


def test_signed_statement_adjustments_and_summaries_remain_source_numbers():
    store = store_with(
        (
            "slip",
            "payslip",
            {
                "gross": "-100",
                "deduction": "-10",
                "net": "-90",
                "earning_1_label": "기본급 조정",
                "earning_1_amount": "-100",
                "deduction_1_label": "환급 세액",
                "deduction_1_amount": "-10",
            },
        )
    )
    materials = build_materials(store, {"payslip_case_id": "slip"})
    assert materials["suggested"]["manual"] == {"gross": "-100", "deduction": "-10", "net": "-90"}
    assert materials["earnings"][0]["amount"] == "-100"
    result = enrich_snapshot(
        snapshot(), materials, {"deductions": {materials["deductions"][0]["item_id"]: "-5"}}
    )
    assert result.earnings[0].amount == -100 and result.deductions[0].amount == -5
    assert result.earnings_complete and result.deductions_complete
    build_calculation_context(result, result.owner_id, result.case_id)


@pytest.mark.parametrize("field_id", ["transaction_amount", "amount"])
def test_bank_amount_review_stays_nonnegative(field_id):
    materials = build_materials(basic_store(), {"deposit_case_ids": ["bank"]})
    row = materials["transactions"][0]
    with pytest.raises(ValueError):
        enrich_snapshot(
            snapshot(),
            materials,
            {"transactions": {row["transaction_id"]: {"selected": True, field_id: "-1"}}},
        )


@pytest.mark.parametrize(
    "account1,account2,reference1,reference2,expected_candidate",
    [
        ("123-456", "123-456", None, None, True),
        ("123-456", None, None, None, True),
        ("123-456", "999-888", None, None, False),
        ("123-456", "123-456", "TX-001", "TX-002", False),
    ],
)
def test_corrected_original_amount_rechecks_duplicate_candidates_before_summing(
    account1, account2, reference1, reference2, expected_candidate
):
    values1 = {
        "account_number": account1,
        "transaction_1_date": "2026-10-25",
        "transaction_1_deposit_amount": "90",
        "transaction_1_reference": reference1,
    }
    values2 = {
        "account_number": account2,
        "transaction_1_date": "2026-10-25",
        "transaction_1_deposit_amount": "9",
        "transaction_1_reference": reference2,
    }
    store = store_with(("bank1", "deposit", values1), ("bank2", "deposit", values2))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    before = copy.deepcopy(materials)
    rows = materials["transactions"]
    assert all(row["duplicate_candidate_group"] is None for row in rows)
    assert rows[0]["account_number"] == account1 and rows[0]["reference"] == reference1
    review = {
        "transactions": {
            rows[0]["transaction_id"]: {"selected": True, "amount": "90"},
            rows[1]["transaction_id"]: {"selected": True, "amount": "90", "transaction_amount": "90"},
        }
    }
    groups = reviewed_duplicate_groups(materials, review)
    if expected_candidate:
        assert (
            groups[rows[0]["transaction_id"]]
            and groups[rows[0]["transaction_id"]] == groups[rows[1]["transaction_id"]]
        )
    else:
        assert not any(groups.values())
    result = enrich_snapshot(snapshot(), materials, review)
    assert len(result.transaction_allocations) == 2
    assert result.payment_scope_confirmed is not expected_candidate
    review["distinct_transactions_confirmed"] = True
    assert enrich_snapshot(snapshot(), materials, review).payment_scope_confirmed
    assert materials == before


def test_correction_can_remove_original_false_duplicate_hint():
    values = {"transaction_1_date": "2026-10-25", "transaction_1_deposit_amount": "90"}
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    rows = materials["transactions"]
    assert all(row["duplicate_candidate_group"] for row in rows)
    review = {
        "transactions": {
            rows[0]["transaction_id"]: {"selected": True},
            rows[1]["transaction_id"]: {"selected": True, "amount": "9", "transaction_amount": "9"},
        }
    }
    assert not any(reviewed_duplicate_groups(materials, review).values())
    assert enrich_snapshot(snapshot(), materials, review).payment_scope_confirmed


def test_null_original_correction_uses_source_amount_for_duplicate_hint():
    values = {"transaction_1_date": "2026-10-25", "transaction_1_deposit_amount": "90"}
    store = store_with(("bank1", "deposit", values), ("bank2", "deposit", values))
    materials = build_materials(store, {"deposit_case_ids": ["bank1", "bank2"]})
    row = materials["transactions"][1]
    assert all(
        reviewed_duplicate_groups(
            materials, {"transactions": {row["transaction_id"]: {"transaction_amount": None}}}
        ).values()
    )


def test_contract_dates_never_suggest_salary_period_and_payslip_unit_sets_profile():
    store = store_with(
        ("contract", "contract", {"contract_start": "2026-01-01", "contract_end": "2027-12-31"}),
        ("slip", "payslip", {"slip_rate": ("3000000", "KRW/month")}),
    )
    assert build_materials(store, {"contract_case_id": "contract"})["suggested"]["period_start"] is None
    materials = build_materials(store, {"payslip_case_id": "slip"})
    assert materials["suggested"]["slip_unit"] == "KRW/month"
    confirmed = snapshot()
    next(field for field in confirmed.confirmed_fields if field.field_id == "slip_rate").unit = "KRW/month"
    assert enrich_snapshot(confirmed, materials, {}).profile == "M-STD"


def test_original_periods_attached_to_corresponding_confirmed_fields():
    store = store_with(
        ("slip", "payslip", {"period_start": "2026-09-01", "period_end": "2026-09-30", "gross": "100"}),
        (
            "work",
            "attendance",
            {"period_start": "2026-08-01", "period_end": "2026-08-31", "worked_minutes": ("480", "minute")},
        ),
    )
    materials = build_materials(store, {"payslip_case_id": "slip", "attendance_case_ids": ["work"]})
    result = enrich_snapshot(snapshot(), materials, {})
    fields = {field.field_id: field for field in result.confirmed_fields}
    assert fields["gross"].period.start.isoformat() == "2026-09-01"
    assert fields["net"].period.start.isoformat() == "2026-09-01"
    assert fields["worked_minutes"].period.start.isoformat() == "2026-08-01"


@pytest.mark.parametrize(
    "second_start,second_end,expected_state",
    [
        ("2026-10-16", "2026-10-31", "known"),
        ("2026-10-15", "2026-10-31", "conflict"),
        ("2026-10-17", "2026-10-31", "conflict"),
        ("2026-10-16", "2026-11-01", "conflict"),
        (None, None, "conflict"),
    ],
)
def test_multiple_attendance_never_auto_sum_and_require_exact_nonoverlapping_coverage(
    second_start, second_end, expected_state
):
    store = store_with(
        (
            "work1",
            "attendance",
            {"period_start": "2026-10-01", "period_end": "2026-10-15", "worked_minutes": ("240", "minute")},
        ),
        (
            "work2",
            "attendance",
            {"period_start": second_start, "period_end": second_end, "worked_minutes": ("240", "minute")},
        ),
    )
    materials = build_materials(store, {"attendance_case_ids": ["work1", "work2"]})
    assert "worked_minutes" not in materials["suggested"]["manual"]
    assert len(materials["field_evidence"]["worked_minutes"]) == 2
    result = enrich_snapshot(snapshot(), materials, {})
    field = next(field for field in result.confirmed_fields if field.field_id == "worked_minutes")
    assert field.state == expected_state
    assert field.value == (480 if expected_state == "known" else None)
    assert next(field for field in result.confirmed_fields if field.field_id == "gross").state == "known"
