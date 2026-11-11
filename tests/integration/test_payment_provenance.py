"""Payment provenance travels through real extraction, ledger, mapping and rules."""

import pytest
import json
from types import SimpleNamespace

from agent.schemas.public.errors import AccessRevoked
from agent.tests.fixtures_support import pdf_bytes
from app.agent_mapping import prepare_transaction_provenance
from app.db import load
from app.errors import ApiError
from app.routers.payments import insert_allocation, insert_transaction
from app.routers.documents import delete_document, document_impact
from test_runtime_db import Harness


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "payment-provenance")


def payment_confirmation(ids):
    return {"profile": "H-STD", "subjects_confirmed": True, "payment_scope_confirmed": True,
        "selected_transaction_ids": ids, "confirmed_fields": [{
            "field_id": "net", "value": "1800000", "unit": "KRW", "confirmation_id": "user-net",
            "state": "known", "period": {"start": "2026-04-01", "end": "2026-04-30"}}]}


async def test_document_row_provenance_reaches_real_bank_comparison(h):
    quote = "2026-04-30 PAYROLL-REF-001 1800000 KRW"
    doc_id = h.document(pdf_bytes(["SYNTHETIC BANK DEPOSIT", quote]), cid=1)

    class DepositProvider:
        async def invoke(self, operation, data):
            assert operation == "extract"
            return {"document_type": "deposit", "issues": [], "fields": [{
                "field_id": "transaction_1_" + suffix, "raw_value": value,
                "normalized_value": value, "unit": unit, "quote": quote, "page": 1, "issues": [],
            } for suffix, value, unit in (("date", "2026-04-30", "date"),
                ("reference", "PAYROLL-REF-001", "text"), ("deposit_amount", "1800000", "KRW"))]}, {}

    h.service.provider = DepositProvider()
    assert (await h.execute(h.service.enqueue_read(1, doc_id)))["status"] == "COMPLETED"
    extraction = await h.execute(h.service.enqueue_extraction(1, doc_id, document_type="deposit"))
    assert extraction["status"] == "COMPLETED", extraction
    candidate = load(extraction["output_json"])
    row_fields = {item["field_id"]: item for item in candidate["candidate"]["fields"]}
    assert row_fields["transaction_1_deposit_amount"]["validation_status"] == "valid", row_fields
    data = {"paymentMethod": "BANK_TRANSFER", "transactionDate": "2026-04-30", "amount": 1800000,
        "source": "DOCUMENT", "confirmationStatus": "USER_CONFIRMED", "sourceDocumentId": doc_id,
        "sourceCandidateId": candidate["candidate_id"], "sourceRowId": "transaction_1"}
    with h.db.connect(write=True) as con:
        transaction_id = insert_transaction(con, 1, data)
        insert_allocation(con, 1, transaction_id, 1800000)
        stored = load(con.execute("SELECT provenance_json FROM payment_transactions WHERE id=?", (transaction_id,)).fetchone()[0])
        assert stored["bank_reference"] == "PAYROLL-REF-001"
        assert stored["source_row_id"] == "transaction_1"
        assert stored["evidence_refs"]
        assert stored["verification"] == "source_row_validated"
        unconfirmed = prepare_transaction_provenance(con, 1, data | {"confirmationStatus": "USER_REPORTED"})
        assert unconfirmed["verification"] == "source_row_validated"
        with pytest.raises(ApiError) as sender_error:
            insert_transaction(con, 1, data | {"sender": "Invented sender"})
        assert sender_error.value.code == "TRANSACTION_SENDER_MISMATCH"
        with pytest.raises(ApiError) as duplicate_error:
            insert_transaction(con, 1, data)
        assert duplicate_error.value.code == "DUPLICATE_TRANSACTION"
        assert duplicate_error.value.details["existingTransactionId"] == transaction_id
    h.case(2)
    with h.db.connect(write=True) as con, pytest.raises(ApiError) as allocation_error:
        insert_allocation(con, 2, transaction_id, 1)
    assert allocation_error.value.code == "ALLOCATION_EXCEEDS_TRANSACTION"
    h.group("source-payment", payment_confirmation([transaction_id]))
    h.select("source-payment", doc_id)
    result, snapshot, _ = await h.analysis("source-payment")
    comparison = next(item for item in result.comparisons if item.rule_id == "R-06")
    assert comparison.status == "match"
    assert set(stored["evidence_refs"]) <= set(comparison.evidence_refs)
    assert set(stored["evidence_refs"]) <= {item["evidence_id"] for item in snapshot["evidence_catalog"]}
    with h.db.connect(write=True) as con:
        con.execute("UPDATE allocations SET allocated_amount=900000 WHERE transaction_id=?", (transaction_id,))
        insert_allocation(con, 2, transaction_id, 900000)
        manual_id = insert_transaction(con, 1, {"paymentMethod": "BANK_TRANSFER", "transactionDate": "2026-04-30",
            "amount": 100, "source": "USER_INPUT", "confirmationStatus": "USER_CONFIRMED"})
        before_versions = {row["id"]: row["input_version"] for row in con.execute("SELECT id,input_version FROM cases")}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=h.db,
        settings=SimpleNamespace(recompute_debounce_seconds=0))))
    impact = json.loads(document_impact(1, doc_id, request, user={"id": 1}).body)["data"]
    assert set(impact["affectedCaseIds"]) == {1, 2}
    delete_document(1, doc_id, request, confirm=True, user={"id": 1})
    with h.db.connect() as con:
        assert con.execute("SELECT 1 FROM payment_transactions WHERE id=?", (transaction_id,)).fetchone() is None
        assert con.execute("SELECT 1 FROM allocations WHERE transaction_id=?", (transaction_id,)).fetchone() is None
        assert con.execute("SELECT 1 FROM payment_transactions WHERE id=?", (manual_id,)).fetchone()
        assert all(row["input_version"] > before_versions[row["id"]] for row in con.execute("SELECT id,input_version FROM cases"))
    with h.db.connect() as con, pytest.raises(AccessRevoked):
        h.store.result(con, 1, result.result_id)


def test_caller_document_label_is_not_verified_provenance(h):
    with h.db.connect(write=True) as con, pytest.raises(ApiError) as exc:
        insert_transaction(con, 1, {"paymentMethod": "BANK_TRANSFER", "transactionDate": "2026-04-30",
            "amount": 1800000, "source": "DOCUMENT", "confirmationStatus": "USER_CONFIRMED"})
    assert exc.value.code == "DOCUMENT_TRANSACTION_PROVENANCE_REQUIRED"


async def test_distinct_confirmed_bank_references_separate_same_date_amount(h):
    ids = []
    with h.db.connect(write=True) as con:
        for reference in ("TRANSFER-A", "TRANSFER-B"):
            tid = insert_transaction(con, 1, {"paymentMethod": "BANK_TRANSFER", "transactionDate": "2026-04-30",
                "amount": 900000, "source": "USER_INPUT", "confirmationStatus": "USER_CONFIRMED", "bankReference": reference})
            insert_allocation(con, 1, tid, 900000)
            ids.append(tid)
    h.group("distinct-references", payment_confirmation(ids))
    result, snapshot, _ = await h.analysis("distinct-references")
    assert not any(item["duplicate"] for item in snapshot["transaction_allocations"])
    assert next(item for item in result.comparisons if item.rule_id == "R-06").status == "match"
