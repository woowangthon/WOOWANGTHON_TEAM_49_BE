"""Adversarial access, source-scope and immutable-idempotency regression checks."""

import pytest

from agent.schemas.public.errors import AccessRevoked, IntegrityError
from agent.services.calculation_context import build_calculation_context
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.test_standard_wages import standard_snapshot
from app.agent_mapping import snapshot_from_group
from app.db import load
from app.errors import ApiError
from app.integration_store import source_ref
from test_runtime_db import Harness, confirmation_from


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / "adversarial")


async def test_unlink_relink_cannot_restore_old_result_job_or_artifact_access(h):
    h.group("relink", confirmation_from(standard_snapshot("H-STD")))
    doc_id = h.document(cid=1)
    h.select("relink", doc_id)
    result, _, analysis_job = await h.analysis("relink")
    explanation = await h.execute(h.service.enqueue_explanation(1, result.result_id))
    report = await h.execute(h.service.enqueue_report(1, result.result_id, explanation["id"],
                                                     selected_document_ids=[doc_id]))
    assert report["status"] == "COMPLETED", report
    artifact_id = load(report["output_json"])["artifacts"][0]["artifact_id"]
    h.store.artifact(1, artifact_id)
    with h.db.connect(write=True) as con:
        old = source_ref(con, doc_id, 1, 1)
        con.execute("DELETE FROM document_links WHERE case_id=1 AND document_id=?", (doc_id,))
        con.execute("INSERT INTO document_links(case_id,document_id) VALUES(1,?)", (doc_id,))
        new = source_ref(con, doc_id, 1, 1)
        assert new.link_id != old.link_id
    with h.db.connect() as con, pytest.raises(AccessRevoked):
        h.store.result(con, 1, result.result_id)
    for job_id in (analysis_job, explanation["id"], report["id"]):
        with pytest.raises(AccessRevoked):
            h.store.job(1, job_id)
    with pytest.raises(AccessRevoked):
        h.store.artifact(1, artifact_id)


@pytest.mark.parametrize("location", ["scalar", "wage"])
def test_context_document_cannot_supply_payroll_confirmation(h, location):
    doc_id = h.document(cid=1)
    confirmed = confirmation_from(standard_snapshot("H-STD"))
    confirmed["evidence_catalog"] = [{"evidence_id": "context-citation", "source_id": f"doc:{doc_id}",
        "page": 1, "verification_method": "manual_confirmed", "quote": "User reviewed context"}]
    if location == "scalar":
        confirmed["confirmed_fields"][0]["evidence_refs"] = ["context-citation"]
    else:
        confirmed["wage_conditions"]["ordinary_hourly_rate"]["evidence_refs"] = ["context-citation"]
    h.group("context", confirmed)
    h.select("context", doc_id)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE document_links SET purpose='CONTEXT' WHERE document_id=?", (doc_id,))
        assert source_ref(con, doc_id, 1, 1).usage == "context"
        with pytest.raises(IntegrityError, match="Context-only"):
            snapshot_from_group(con, "context", 1, "reject-context")


def test_context_work_records_keep_context_role_and_break_intervals(h):
    doc_id = h.document(cid=1)
    confirmed = confirmation_from(standard_snapshot("H-STD"))
    confirmed["evidence_catalog"] = [{"evidence_id": "context-citation", "source_id": f"doc:{doc_id}",
        "page": 1, "verification_method": "manual_confirmed", "quote": "User reviewed context"}]
    for record in confirmed["work_records"]:
        record["evidence_refs"] = ["context-citation"]
    h.group("context-records", confirmed)
    h.select("context-records", doc_id)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE document_links SET purpose='context' WHERE document_id=?", (doc_id,))
        snapshot = snapshot_from_group(con, "context-records", 1, "context-records-snapshot")
    context = build_calculation_context(snapshot, "1", "1")
    assert context.normalized_intervals
    assert all(not item["payroll"] for item in context.normalized_intervals)
    assert any(item["on_break"] for item in context.normalized_intervals)
    assert len(snapshot.work_records) == len(confirmed["work_records"])


async def test_supplement_idempotency_cannot_rebind_to_another_base_candidate(h):
    doc_id = h.document()
    reading = await h.execute(h.service.enqueue_read(1, doc_id))
    assert reading["status"] == "COMPLETED"

    class UncertainPrimary(FixtureProvider):
        async def invoke(self, operation, data):
            response, usage = await super().invoke(operation, data)
            response["fields"][0]["quote"] = "Unmatched quote requires review"
            return response, usage

    h.service.provider = UncertainPrimary()
    first = await h.execute(h.service.enqueue_extraction(1, doc_id, document_type="payslip"))
    first_candidate = load(first["output_json"])
    field = next(item for item in first_candidate["candidate"]["fields"] if item["validation_status"] == "needs_review")
    targets = [{"field_id": field["field_id"], "page": 1}]
    old_b = h.service.enqueue_extraction(1, doc_id, method="pdf_supplement", target_fields=targets,
        idempotency_key="same-b-request")
    previous = h.row("agent_jobs", old_b)["metadata_json"]
    # A different explicitly requested A can finish while the old B is pending.
    second = await h.execute(h.service.enqueue_extraction(1, doc_id, document_type="payslip"))
    assert second["status"] == "COMPLETED"
    assert load(second["output_json"])["candidate_id"] != first_candidate["candidate_id"]
    with pytest.raises(ApiError) as exc:
        h.service.enqueue_extraction(1, doc_id, method="pdf_supplement", target_fields=targets,
            idempotency_key="same-b-request")
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert h.row("agent_jobs", old_b)["metadata_json"] == previous
    cancelled = await h.execute(old_b)
    assert cancelled["status"] != "COMPLETED"
    with h.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM extraction_candidates WHERE job_id=?", (old_b,)).fetchone()[0] == 0


async def test_workplace_owner_change_revokes_completed_job_outputs(h):
    h.group("ownership", confirmation_from(standard_snapshot("H-STD")))
    result, _, job_id = await h.analysis("ownership")
    with h.db.connect(write=True) as con:
        con.execute("UPDATE workplaces SET user_id=2 WHERE id=1")
    with pytest.raises(AccessRevoked):
        h.store.job(1, job_id)
    with pytest.raises(ApiError):
        h.store.job(2, job_id)
    with h.db.connect() as con, pytest.raises(AccessRevoked):
        h.store.result(con, 1, result.result_id)
