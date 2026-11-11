"""User confirmations over immutable A/B candidates, separate from group inputs."""

from copy import deepcopy

from agent.services.candidate_selection import select_candidate
from agent.services.extraction_profiles import field_definition
from app.db import dump, load, now
from app.errors import ApiError
from app.integration_store import identifier, owned_document, source_ref


SCHEMA = """
CREATE TABLE IF NOT EXISTS document_confirmations (
 id TEXT PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 owner_id INTEGER NOT NULL REFERENCES users(id), field_id TEXT NOT NULL,
 candidate_id TEXT REFERENCES extraction_candidates(id), document_version INTEGER NOT NULL,
 document_hash TEXT NOT NULL, ocr_result_id TEXT, event TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_confirmation_history ON document_confirmations(document_id,created_at);
CREATE TRIGGER IF NOT EXISTS immutable_document_confirmation BEFORE UPDATE ON document_confirmations
BEGIN SELECT RAISE(ABORT,'Document confirmation history is immutable'); END;
"""


def initialize_review(db):
    with db.connect(snapshot=False) as con:
        con.executescript(SCHEMA)


def invalidate_document_confirmations(con, document_id, reason):
    """Preserve original values and audit, requiring confirmation of the new source."""
    doc = con.execute("SELECT d.*,w.user_id FROM documents d JOIN workplaces w ON w.id=d.workplace_id WHERE d.id=?",
                      (document_id,)).fetchone()
    if doc is None:
        return
    rows = con.execute("SELECT * FROM fields WHERE document_id=? AND (status='CONFIRMED' OR edited=1)",
                       (document_id,)).fetchall()
    for row in rows:
        if row["status"] == "NEEDS_RECONFIRMATION":
            continue
        previous = {"value": load(row["value_json"]), "status": row["status"], "source": load(row["source_json"])}
        con.execute("""INSERT INTO document_confirmations(id,document_id,owner_id,field_id,document_version,
            document_hash,event,data_json,created_at) VALUES(?,?,?,?,?,?,'invalidated',?,?)""",
            (identifier(), document_id, doc["user_id"], row["key"], doc["document_version"], doc["sha256"],
             dump({"reason": reason, "previous": previous}), now()))
        con.execute("UPDATE fields SET status='NEEDS_RECONFIRMATION' WHERE id=?", (row["id"],))


def _candidate_context(con, owner_id, document_id, candidate_id):
    doc = owned_document(con, document_id, owner_id)
    ref = source_ref(con, document_id, owner_id)
    reading = con.execute("""SELECT * FROM document_ocr WHERE document_id=? AND document_version=?
        AND content_hash=? ORDER BY created_at DESC,id DESC LIMIT 1""",
        (document_id, doc["document_version"], ref.content_hash)).fetchone()
    base = con.execute("""SELECT * FROM extraction_candidates WHERE document_id=? AND document_version=?
        AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1""",
        (document_id, doc["document_version"])).fetchone()
    if reading is None or base is None or base["ocr_result_id"] != reading["id"]:
        raise ApiError(409, "CURRENT_BASE_EXTRACTION_REQUIRED")
    selected = con.execute("SELECT * FROM extraction_candidates WHERE id=? AND document_id=?",
                           (candidate_id, document_id)).fetchone()
    if selected is None:
        raise ApiError(404, "CANDIDATE_NOT_FOUND")
    if (selected["document_version"] != doc["document_version"] or selected["ocr_result_id"] != reading["id"]
            or (selected["method"] == "text" and selected["id"] != base["id"])
            or (selected["method"] == "pdf_supplement" and selected["base_candidate_id"] != base["id"])):
        raise ApiError(409, "CANDIDATE_LINEAGE_CHANGED")
    base_job = con.execute("SELECT * FROM agent_jobs WHERE id=?", (base["job_id"],)).fetchone()
    revision = con.execute("SELECT COUNT(*) FROM extraction_candidates WHERE document_id=? AND method='text'",
                           (document_id,)).fetchone()[0]
    choice_count = con.execute("SELECT COUNT(*) FROM document_confirmations WHERE document_id=?",
                               (document_id,)).fetchone()[0]
    base_payload = load(base["data_json"])
    case = {"extraction": deepcopy(base_payload), "extraction_a": deepcopy(base_payload),
        "extraction_revision": revision + choice_count, "extraction_a_revision": revision,
        "extraction_a_context": {"document_hash": ref.content_hash, "ocr_result_id": reading["id"],
            "job_id": base["job_id"], "run_id": base_job["run_id"]},
        "ocr": {"id": reading["id"], "content_hash": ref.content_hash},
        "candidate_selections": {}, "candidate_selection_history": [], "supplements": []}
    for item in con.execute("""SELECT e.*,j.run_id FROM extraction_candidates e JOIN agent_jobs j ON j.id=e.job_id
        WHERE e.document_id=? AND e.base_candidate_id=? AND e.ocr_result_id=? AND e.document_version=?""",
        (document_id, base["id"], reading["id"], doc["document_version"])):
        case["supplements"].append({"id": item["id"], "base_extraction_revision": revision,
            "ocr_result_id": reading["id"], "document_hash": ref.content_hash, "run_id": item["run_id"],
            "payload": load(item["data_json"])})
    return doc, ref, base, selected, case


def confirm_candidate(con, owner_id, document_id, candidate_id, field_id, *, value=None, unit=None,
                      manual=False, defer=False):
    """Validate current candidate provenance and append a user confirmation.

Call inside the API's short write transaction. This writes neither source bytes,
AI candidates nor existing group confirmations; groups must explicitly reload.
"""
    if manual and defer:
        raise ApiError(422, "INVALID_CONFIRMATION")
    doc, ref, base, selected, case = _candidate_context(con, owner_id, document_id, candidate_id)
    field = next((item for item in case["extraction_a"]["fields"] if item["field_id"] == field_id), None)
    if field is None:
        raise ApiError(422, "FIELD_NOT_FOUND_IN_BASE")
    definition = field_definition(case["extraction_a"]["document_type"], field_id) or {}
    existing = con.execute("SELECT * FROM fields WHERE document_id=? AND key=?", (document_id, field_id)).fetchone()
    previous = {"value": load(existing["value_json"]), "status": existing["status"],
                "source": load(existing["source_json"])} if existing else None
    if defer:
        record = {"event": "deferred", "field_id": field_id, "candidate_id": candidate_id,
            "value": None, "unit": field.get("unit"), "document_hash": ref.content_hash,
            "ocr_result_id": base["ocr_result_id"], "base_candidate_id": base["id"], "previous": previous}
        chosen = deepcopy(field) | {"normalized_value": None, "evidence_refs": []}
        status = "DEFERRED"
    else:
        choice = "manual" if manual else "A" if selected["id"] == base["id"] else selected["id"] + ":" + field_id
        try:
            effective, record = select_candidate(case, {"field_id": field_id, "candidate_id": choice,
                "value": value, "unit": unit, "source_confirmed": True,
                "extraction_revision": case["extraction_revision"]},
                document_hash=ref.content_hash, source_id=ref.source_id)
        except ValueError as exc:
            raise ApiError(422, "CANDIDATE_CONFIRMATION_INVALID", str(exc)) from exc
        chosen = next(item.model_dump(mode="json") for item in effective.fields if item.field_id == field_id)
        record.update(base_candidate_id=base["id"], previous=previous,
                      evidence_catalog=[item.model_dump(mode="json") for item in effective.evidence_catalog])
        status = "CONFIRMED"
    record.update(owner_id=str(owner_id), document_version=doc["document_version"], document_id=document_id)
    audit_id, created_at = identifier(), now()
    con.execute("""INSERT INTO document_confirmations(id,document_id,owner_id,field_id,candidate_id,
        document_version,document_hash,ocr_result_id,event,data_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (audit_id, document_id, owner_id, field_id, selected["id"], doc["document_version"], ref.content_hash,
         base["ocr_result_id"], record["event"], dump(record), created_at))
    projection = {"candidateId": selected["id"], "baseCandidateId": base["id"],
        "confirmationId": audit_id, "documentVersion": doc["document_version"],
        "documentHash": ref.content_hash, "ocrResultId": base["ocr_result_id"],
        "evidenceRefs": chosen["evidence_refs"], "unit": chosen["unit"]}
    con.execute("""INSERT INTO fields(document_id,key,label,value_json,value_type,status,edited,confirmed_at,source_json)
        VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(document_id,key) DO UPDATE SET value_json=excluded.value_json,
        status=excluded.status,edited=1,confirmed_at=excluded.confirmed_at,source_json=excluded.source_json""",
        (document_id, field_id, definition.get("label", field_id), dump(chosen["normalized_value"]),
         "TEXT", status, created_at if not defer else None, dump(projection)))
    con.execute("""UPDATE comparison_groups SET needs_review=1,version=version+1,updated_at=?
        WHERE id IN (SELECT group_id FROM group_documents WHERE document_id=?)""", (created_at, document_id))
    return {"confirmation_id": audit_id, "status": status, "field": chosen, "audit": record}


def confirmation_history(con, owner_id, document_id):
    owned_document(con, document_id, owner_id)
    return [{"confirmation_id": row["id"], "field_id": row["field_id"], "event": row["event"],
        "candidate_id": row["candidate_id"], "document_version": row["document_version"],
        "created_at": row["created_at"], "audit": load(row["data_json"])}
        for row in con.execute("SELECT * FROM document_confirmations WHERE document_id=? ORDER BY created_at,id",
                               (document_id,))]
