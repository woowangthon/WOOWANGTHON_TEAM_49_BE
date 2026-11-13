"""Explicit user attestations of source table coverage, without confirming OCR amounts."""
from agent.services.hashing import digest


def scope_hash(con, case_id, document_id):
    from app.engine import effective_fields
    doc = con.execute("SELECT sha256,document_version FROM documents WHERE id=?", (document_id,)).fetchone()
    if doc is None or not con.execute("SELECT 1 FROM document_links l JOIN documents d ON d.id=l.document_id WHERE l.case_id=? AND d.id=? AND d.deleted_at IS NULL", (case_id, document_id)).fetchone():
        return None
    return digest({"document": list(doc), "fields": effective_fields(con, case_id, document_id)})
