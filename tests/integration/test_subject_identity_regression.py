"""Bank account holders retain their identity checks in confirmed group inputs."""

import pytest

from agent.schemas.public.results import ExtractionCandidate
from agent.tests.fixtures_support import pdf_bytes
from app.agent_mapping import group_materials, snapshot_from_group
from app.agent_review import confirm_candidate, initialize_review
from app.db import dump, now
from app.errors import ApiError
from test_runtime_db import Harness


@pytest.fixture
def identity_harness(tmp_path):
    harness = Harness(tmp_path / "subject-identity")
    initialize_review(harness.db)
    return harness


def stored_document(harness, kind, values):
    """Seed an immutable, schema-valid saved extraction without calling a model."""
    document_id = harness.document(
        pdf_bytes(["SYNTHETIC " + kind.upper(), *(key + ": " + value for key, value in values.items())]),
        cid=1,
    )
    read_id, extract_id = f"read-{document_id}", f"extract-{document_id}"
    reading_id, candidate_id = f"reading-{document_id}", f"candidate-{document_id}"
    candidate = ExtractionCandidate(
        document_id=str(document_id), document_version=1, document_type=kind,
        extraction_version="1.2", model_config_version="luna-v1", fields=[{
            "field_id": key, "raw_value": value, "normalized_value": value,
            "unit": "text", "evidence_refs": [], "source_page": 1,
            "source_quote": key + ": " + value,
        } for key, value in values.items()], evidence_catalog=[], issues=[], page_count=1,
        verification_method="text_match", source_method="pdf_text", page_methods=["pdf_text"],
    )
    with harness.db.connect(write=True) as con:
        con.execute("UPDATE documents SET document_type=? WHERE id=?", (kind.upper(), document_id))
        for job_id, task_kind in ((read_id, "read_document"), (extract_id, "extract_document")):
            con.execute("""INSERT INTO agent_jobs(id,owner_id,document_id,task_kind,payload_json,
                payload_hash,idempotency_key,status,run_id,created_at) VALUES(?,1,?,?,?, ?,?,'COMPLETED',?,?)""",
                (job_id, document_id, task_kind, "{}", "0" * 64, job_id, "run-" + job_id, now()))
        original_hash = con.execute("SELECT sha256 FROM documents WHERE id=?", (document_id,)).fetchone()[0]
        con.execute("""INSERT INTO document_ocr(id,job_id,document_id,document_version,source_id,
            content_hash,data_json,created_at) VALUES(?,?,?,1,?,?,?,?)""",
            (reading_id, read_id, document_id, f"doc:{document_id}", original_hash, "{}", now()))
        con.execute("""INSERT INTO extraction_candidates(id,job_id,document_id,document_version,
            ocr_result_id,method,data_json,created_at) VALUES(?,?,?,1,?,'text',?,?)""",
            (candidate_id, extract_id, document_id, reading_id, candidate.model_dump_json(), now()))
    return document_id, candidate_id


def selected_pair(harness, *, holder, employee_name=None, source_identity=None):
    payslip_id, _ = stored_document(harness, "payslip", {"employee_name": "Alice", "employer_name": "Acme"})
    deposit_values = {"account_holder": holder}
    if employee_name is not None:
        deposit_values["employee_name"] = employee_name
    deposit_id, candidate_id = stored_document(harness, "deposit", deposit_values)
    confirmed = {"subjects_confirmed": True}
    if source_identity is not None:
        confirmed["source_identity"] = {str(deposit_id): source_identity}
    harness.group("identity", confirmed)
    harness.select("identity", payslip_id)
    harness.select("identity", deposit_id)
    return deposit_id, candidate_id


def test_different_account_holder_blocks_blanket_subject_confirmation(identity_harness):
    h = identity_harness
    deposit_id, _ = selected_pair(h, holder="Bob")
    with h.db.connect() as con:
        materials = group_materials(con, "identity", 1)
        deposit = next(source for source in materials["sources"] if source["document_id"] == str(deposit_id))
        assert deposit["identity"]["employee_name"] == "Bob"
        with pytest.raises(ApiError) as error:
            snapshot_from_group(con, "identity", 1, "different-holder")
    assert error.value.code == "SOURCE_SUBJECT_REVIEW_REQUIRED"
    assert any(issue["code"] == "SUBJECT_MISMATCH" for issue in error.value.details["issues"])


def test_same_account_holder_allows_confirmed_group(identity_harness):
    h = identity_harness
    selected_pair(h, holder="Alice")
    with h.db.connect() as con:
        snapshot = snapshot_from_group(con, "identity", 1, "same-holder")
    assert snapshot.case_id == "1"


def test_explicit_group_identity_correction_resolves_holder_conflict(identity_harness):
    h = identity_harness
    deposit_id, candidate_id = selected_pair(h, holder="Bob", source_identity={"employee_name": "Alice"})
    before = h.row("extraction_candidates", candidate_id)["data_json"]
    with h.db.connect() as con:
        snapshot = snapshot_from_group(con, "identity", 1, "corrected-group-identity")
        assert not group_materials(con, "identity", 1)["compatibility"]["blocked"]
    assert str(deposit_id) in {source.document_id for source in snapshot.authorized_sources}
    assert h.row("extraction_candidates", candidate_id)["data_json"] == before


def test_confirmed_account_holder_edit_supplies_identity_fallback(identity_harness):
    h = identity_harness
    deposit_id, candidate_id = selected_pair(h, holder="Bob")
    before = h.row("extraction_candidates", candidate_id)["data_json"]
    with h.db.connect(write=True) as con:
        choice = confirm_candidate(con, 1, deposit_id, candidate_id, "account_holder", manual=True, value="Alice")
        assert choice["status"] == "CONFIRMED"
        materials = group_materials(con, "identity", 1)
        deposit = next(source for source in materials["sources"] if source["document_id"] == str(deposit_id))
        assert deposit["identity"]["employee_name"] == "Alice"
        # Document review invalidates the group; the caller explicitly reconfirms its inputs.
        con.execute("UPDATE comparison_groups SET confirmed_json=?,needs_review=0 WHERE id='identity'",
                    (dump({"subjects_confirmed": True}),))
        snapshot = snapshot_from_group(con, "identity", 1, "reviewed-holder")
    assert snapshot.case_id == "1"
    assert h.row("extraction_candidates", candidate_id)["data_json"] == before


def test_explicit_employee_name_takes_precedence_over_account_holder(identity_harness):
    h = identity_harness
    deposit_id, _ = selected_pair(h, holder="Bob", employee_name="Alice")
    with h.db.connect() as con:
        snapshot_from_group(con, "identity", 1, "employee-name-precedence")
        deposit = next(source for source in group_materials(con, "identity", 1)["sources"]
                       if source["document_id"] == str(deposit_id))
    assert deposit["identity"]["employee_name"] == "Alice"
