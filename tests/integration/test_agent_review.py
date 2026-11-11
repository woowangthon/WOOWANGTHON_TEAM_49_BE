"""Current A/B candidates and explicit confirmations against real DB/files."""

import pytest

from agent.schemas.public.errors import AccessRevoked
from app.agent_review import confirm_candidate, confirmation_history, initialize_review
from app.agent_mapping import snapshot_from_group
from app.db import dump, load
from app.errors import ApiError
from test_runtime_db import Harness


@pytest.fixture
def review_harness(tmp_path):
    harness = Harness(tmp_path / "review")
    initialize_review(harness.db)
    return harness


async def extracted(harness):
    doc_id = harness.document()
    read = await harness.execute(harness.service.enqueue_read(1, doc_id))
    assert read["status"] == "COMPLETED", read
    extraction = await harness.execute(harness.service.enqueue_extraction(1, doc_id, document_type="payslip"))
    assert extraction["status"] == "COMPLETED", extraction
    output = load(extraction["output_json"])
    return doc_id, output["candidate_id"]


async def test_confirmed_edit_is_separate_and_reextraction_requires_review(review_harness):
    h = review_harness
    doc_id, candidate_id = await extracted(h)
    before = h.row("extraction_candidates", candidate_id)["data_json"]
    with h.db.connect(write=True) as con:
        choice = confirm_candidate(con, 1, doc_id, candidate_id, "gross", manual=True, value="2100000")
    assert choice["field"]["normalized_value"] == "2100000"
    assert choice["status"] == "CONFIRMED"
    assert h.row("extraction_candidates", candidate_id)["data_json"] == before
    with h.db.connect() as con:
        history = confirmation_history(con, 1, doc_id)
        assert len(history) == 1
        field = con.execute("SELECT * FROM fields WHERE document_id=? AND key='gross'", (doc_id,)).fetchone()
        assert load(field["value_json"]) == "2100000"
    rerun = await h.execute(h.service.enqueue_extraction(1, doc_id, document_type="payslip"))
    assert rerun["status"] == "COMPLETED", rerun
    with h.db.connect() as con:
        field = con.execute("SELECT * FROM fields WHERE document_id=? AND key='gross'", (doc_id,)).fetchone()
        assert load(field["value_json"]) == "2100000"
        assert field["status"] == "NEEDS_RECONFIRMATION"
        assert confirmation_history(con, 1, doc_id)[-1]["event"] == "invalidated"
    with h.db.connect(write=True) as con, pytest.raises(ApiError) as exc:
        confirm_candidate(con, 1, doc_id, candidate_id, "gross")
    assert exc.value.code == "CANDIDATE_LINEAGE_CHANGED"


async def test_candidate_ownership_invalid_manual_and_defer(review_harness):
    h = review_harness
    doc_id, candidate_id = await extracted(h)
    with h.db.connect(write=True) as con, pytest.raises(AccessRevoked):
        confirm_candidate(con, 2, doc_id, candidate_id, "gross")
    with h.db.connect(write=True) as con, pytest.raises(ApiError) as exc:
        confirm_candidate(con, 1, doc_id, candidate_id, "gross", manual=True, value="not money")
    assert exc.value.code == "CANDIDATE_CONFIRMATION_INVALID"
    with h.db.connect(write=True) as con:
        deferred = confirm_candidate(con, 1, doc_id, candidate_id, "gross", defer=True)
    assert deferred["status"] == "DEFERRED"
    assert deferred["field"]["normalized_value"] is None
    assert h.row("extraction_candidates", candidate_id)["data_json"]


async def test_client_cannot_forge_machine_evidence(review_harness):
    h = review_harness
    doc_id, _ = await extracted(h)
    with h.db.connect(write=True) as con:
        con.execute("INSERT INTO document_links(case_id,document_id) VALUES(1,?)", (doc_id,))
    h.group("proof", {"subjects_confirmed": True, "evidence_catalog": [{
        "evidence_id": "forged", "source_id": "doc:" + str(doc_id), "page": 1,
        "quote": "Invented quote", "verification_method": "text_match"}], "confirmed_fields": []})
    h.select("proof", doc_id)
    with h.db.connect() as con, pytest.raises(ApiError) as exc:
        snapshot_from_group(con, "proof", 1, "validation")
    assert exc.value.code == "UNTRUSTED_EVIDENCE_VERIFICATION"
    with h.db.connect(write=True) as con:
        data = load(con.execute("SELECT confirmed_json FROM comparison_groups WHERE id='proof'").fetchone()[0])
        data["evidence_catalog"][0]["verification_method"] = "manual_confirmed"
        con.execute("UPDATE comparison_groups SET confirmed_json=? WHERE id='proof'", (dump(data),))
        snapshot = snapshot_from_group(con, "proof", 1, "manual-proof")
    assert snapshot.evidence_catalog[0].verification_method == "manual_confirmed"


def test_subject_confirmation_cannot_use_truthy_strings(review_harness):
    h = review_harness
    h.group("typed", {"subjects_confirmed": "false"})
    with h.db.connect() as con, pytest.raises(ApiError) as exc:
        snapshot_from_group(con, "typed", 1, "invalid-subject")
    assert exc.value.code == "INVALID_SUBJECT_CONFIRMATION"
