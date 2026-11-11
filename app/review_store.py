"""Authenticated, SQLite-backed review drafts and fenced document jobs."""

from pathlib import Path
import secrets

from app.db import dump, load, now
from app.errors import ApiError
from app.integration_store import identifier, token_hash
from app.security import case as owned_case
from agent.review.state import decorate, readiness

SCHEMA = '''
CREATE TABLE IF NOT EXISTS review_sessions (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), revision INTEGER NOT NULL DEFAULT 1,
 state_json TEXT NOT NULL, workplace_id INTEGER REFERENCES workplaces(id), case_id INTEGER REFERENCES cases(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS review_documents (
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES review_sessions(id), file_name TEXT NOT NULL,
 storage_name TEXT NOT NULL, media_type TEXT NOT NULL, sha256 TEXT NOT NULL, page_count INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, excluded INTEGER NOT NULL DEFAULT 0,
 data_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(review_id,sha256));
CREATE TABLE IF NOT EXISTS review_jobs (
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES review_sessions(id),
 document_id TEXT NOT NULL REFERENCES review_documents(id), document_revision INTEGER NOT NULL,
 source_hash TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'PENDING', run_id TEXT, execution_hash TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error_code TEXT);
CREATE TABLE IF NOT EXISTS review_events (
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES review_sessions(id), owner_id INTEGER NOT NULL,
 revision INTEGER NOT NULL, event TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_exports (
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES review_sessions(id), revision INTEGER NOT NULL,
 case_id INTEGER NOT NULL REFERENCES cases(id), group_id TEXT NOT NULL REFERENCES comparison_groups(id),
 job_id TEXT REFERENCES agent_jobs(id), snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(review_id,revision));
CREATE TABLE IF NOT EXISTS review_export_documents (
 document_id INTEGER PRIMARY KEY REFERENCES documents(id), review_document_id TEXT NOT NULL REFERENCES review_documents(id),
 export_id TEXT NOT NULL REFERENCES review_exports(id), part_id TEXT NOT NULL,
 start_page INTEGER NOT NULL, end_page INTEGER NOT NULL, original_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS review_jobs_pending ON review_jobs(status,created_at);
CREATE TRIGGER IF NOT EXISTS immutable_review_events BEFORE UPDATE ON review_events
 BEGIN SELECT RAISE(ABORT,'Review history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_exports BEFORE UPDATE OF
 id,review_id,revision,case_id,group_id,snapshot_json,created_at ON review_exports
 BEGIN SELECT RAISE(ABORT,'Review exports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_export_job BEFORE UPDATE OF job_id ON review_exports
 WHEN OLD.job_id IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'Review export jobs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_export_source BEFORE UPDATE ON review_export_documents
 BEGIN SELECT RAISE(ABORT,'Review source ranges are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_job_inputs BEFORE UPDATE OF
 review_id,document_id,document_revision,source_hash,kind,payload_json ON review_jobs
 BEGIN SELECT RAISE(ABORT,'Review job inputs are immutable'); END;
'''


class ReviewStore:
    def __init__(self, db, data_dir):
        self.db, self.directory = db, Path(data_dir) / 'review_uploads'

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.db.connect(snapshot=False) as con:
            con.executescript(SCHEMA)
        with self.db.connect(write=True) as con:
            con.execute("UPDATE review_jobs SET status='INTERRUPTED',execution_hash=NULL,error_code='SERVER_INTERRUPTED' WHERE status='RUNNING'")
            con.execute("UPDATE review_documents SET status='INTERRUPTED' WHERE status='RUNNING'")

    def owned(self, con, owner_id, review_id):
        row = con.execute('''SELECT r.* FROM review_sessions r JOIN users u ON u.id=r.owner_id
            WHERE r.id=? AND r.owner_id=? AND r.deleted_at IS NULL AND u.deleted_at IS NULL''',
            (review_id, owner_id)).fetchone()
        if row is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        if row['case_id']:
            owned_case(con, row['case_id'], owner_id)
        return row

    def version(self, row, expected):
        if type(expected) is not int or expected != row['revision']:
            raise ApiError(409, 'REVIEW_REVISION_CONFLICT', details={'currentRevision': row['revision']})

    def documents(self, con, review_id):
        return [{**dict(row), **load(row['data_json']), 'parts': load(row['data_json']).get('parts', []),
            'excluded': bool(row['excluded'])} for row in con.execute(
                'SELECT * FROM review_documents WHERE review_id=? ORDER BY created_at,id', (review_id,))]

    def document(self, con, review_id, document_id):
        row = con.execute('SELECT * FROM review_documents WHERE id=? AND review_id=?',
            (document_id, review_id)).fetchone()
        if row is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        return {**dict(row), **load(row['data_json']), 'parts': load(row['data_json']).get('parts', []),
            'excluded': bool(row['excluded'])}

    def change(self, con, row, owner_id, event, data, state=None):
        revision = row['revision'] + 1
        con.execute('UPDATE review_sessions SET revision=?,state_json=?,updated_at=? WHERE id=?',
            (revision, dump(state if state is not None else load(row['state_json'])), now(), row['id']))
        con.execute('INSERT INTO review_events VALUES(?,?,?,?,?,?,?)',
            (identifier(), row['id'], owner_id, revision, event, dump(data), now()))
        return revision

    def save_document(self, con, document):
        data = {key: document.get(key, default) for key, default in
            (('parts', []), ('reading', {}), ('history', []))}
        con.execute('UPDATE review_documents SET data_json=?,revision=revision+1,status=?,excluded=?,updated_at=? WHERE id=?',
            (dump(data), document['status'], int(document['excluded']), now(), document['id']))

    def view(self, owner_id, review_id):
        with self.db.connect() as con:
            row = self.owned(con, owner_id, review_id)
            state = load(row['state_json'])
            documents = self.documents(con, review_id)
            ready = readiness(state, documents)
            visible = []
            for document in documents:
                for key in ('storage_name', 'data_json', 'reading'):
                    document.pop(key, None)
                visible.append(decorate(document))
            exports = [dict(item) for item in con.execute('''SELECT id,revision,case_id,group_id,job_id,created_at
                FROM review_exports WHERE review_id=? ORDER BY created_at''', (review_id,))]
            for item in exports:
                item['stale'] = item['revision'] != row['revision']
            return {**state, 'id': review_id, 'revision': row['revision'], 'documents': visible,
                'workplace_id': row['workplace_id'], 'case_id': row['case_id'], 'readiness': ready,
                'exports': exports, 'jobs': [self.job_view(item) for item in con.execute(
                    'SELECT * FROM review_jobs WHERE review_id=? ORDER BY created_at,id', (review_id,))]}

    def job_view(self, row):
        data = load(row['data_json'])
        return {key: row[key] for key in ('id', 'review_id', 'document_id', 'kind', 'status',
            'created_at', 'started_at', 'completed_at', 'error_code')} | {
                'usage': data.get('usage', {}), 'agent_calls': data.get('agent_calls', []),
                'tool_calls': data.get('tool_calls', []), 'execution_trace': data.get('execution_trace', [])}

    def enqueue(self, con, document, kind, payload):
        if con.execute("SELECT 1 FROM review_jobs WHERE document_id=? AND status IN ('PENDING','RUNNING')",
                       (document['id'],)).fetchone():
            raise ApiError(409, 'REVIEW_DOCUMENT_BUSY')
        jid = identifier()
        con.execute('''INSERT INTO review_jobs(id,review_id,document_id,document_revision,source_hash,kind,
            payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)''', (jid, document['review_id'], document['id'],
            document['revision'], document['sha256'], kind, dump(payload), now()))
        con.execute("UPDATE review_documents SET status='PENDING' WHERE id=?", (document['id'],))
        return jid

    def claim(self, job_id=None):
        with self.db.connect(write=True) as con:
            row = con.execute("""SELECT j.* FROM review_jobs j JOIN review_sessions r ON r.id=j.review_id
                JOIN users u ON u.id=r.owner_id WHERE j.status='PENDING' AND j.cancel_requested=0
                AND r.deleted_at IS NULL AND u.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM review_jobs other WHERE other.review_id=j.review_id AND other.status='RUNNING') """
                + ('AND j.id=? ' if job_id else '') + 'ORDER BY j.created_at,j.id LIMIT 1',
                (job_id,) if job_id else ()).fetchone()
            if row is None:
                return None
            run_id, token = identifier(), secrets.token_urlsafe(32)
            con.execute("UPDATE review_jobs SET status='RUNNING',run_id=?,execution_hash=?,started_at=?,error_code=NULL WHERE id=?",
                (run_id, token_hash(token), now(), row['id']))
            con.execute("UPDATE review_documents SET status='RUNNING' WHERE id=?", (row['document_id'],))
            return dict(row) | {'run_id': run_id, 'token': token, 'status': 'RUNNING'}

    def check(self, con, job):
        row = con.execute('SELECT * FROM review_jobs WHERE id=?', (job['id'],)).fetchone()
        if row is None or row['status'] != 'RUNNING' or row['run_id'] != job['run_id'] or not secrets.compare_digest(
            row['execution_hash'] or '', token_hash(job['token'])):
            raise ApiError(409, 'REVIEW_EXECUTION_REVOKED')
        review = con.execute('SELECT owner_id FROM review_sessions WHERE id=?', (job['review_id'],)).fetchone()
        self.owned(con, review['owner_id'], job['review_id'])
        document = self.document(con, job['review_id'], job['document_id'])
        if document['excluded'] or document['sha256'] != job['source_hash']:
            raise ApiError(409, 'REVIEW_SOURCE_CHANGED')
        return document


def validate_part_selection(con, document_ids):
    """The same uploaded bytes may supply distinct parts, never overlapping copies."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_export_documents'").fetchone():
        return
    seen = {}
    for did in document_ids:
        doc = con.execute('SELECT sha256,page_count FROM documents WHERE id=?', (did,)).fetchone()
        if doc is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        part = con.execute('SELECT start_page,end_page FROM review_export_documents WHERE document_id=?', (did,)).fetchone()
        first, last = (part['start_page'], part['end_page']) if part else (1, doc['page_count'] or 1)
        for start, end in seen.setdefault(doc['sha256'], []):
            if first <= end and start <= last:
                raise ApiError(422, 'OVERLAPPING_DOCUMENT_PARTS')
        seen[doc['sha256']].append((first, last))
