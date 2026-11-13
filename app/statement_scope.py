"""Explicit user attestations of source table coverage, without confirming OCR amounts."""
from agent.services.hashing import digest


def scope_state(con, case_id, document_id):
    """Read attestations without refreshing their hashes or confirming values."""
    from app.db import load
    case = con.execute('SELECT conditions,input_version FROM cases WHERE id=?', (case_id,)).fetchone()
    scopes = (load(case['conditions']) or {}).get('statement_scopes', {})
    current_hash = scope_hash(con, case_id, document_id)
    return {
        'documentId': document_id,
        'inputVersion': case['input_version'],
        'statementScopes': scopes,
        'scopeStatus': {
            key: 'UNCONFIRMED' if not scopes.get(key) or scopes[key]['documentId'] != document_id
            else 'CONFIRMED' if scopes[key]['hash'] == current_hash else 'NEEDS_RECONFIRMATION'
            for key in ('paymentItems', 'deductionItems')
        },
    }


def scope_hash(con, case_id, document_id):
    from app.engine import effective_fields
    doc = con.execute("SELECT sha256,document_version FROM documents WHERE id=?", (document_id,)).fetchone()
    if doc is None or not con.execute("SELECT 1 FROM document_links l JOIN documents d ON d.id=l.document_id WHERE l.case_id=? AND d.id=? AND d.deleted_at IS NULL", (case_id, document_id)).fetchone():
        return None
    return digest({"document": list(doc), "fields": effective_fields(con, case_id, document_id)})
