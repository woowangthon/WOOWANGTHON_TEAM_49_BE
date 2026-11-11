"""Durable integration storage. All transactions are short and synchronous.

The agent receives immutable input and returns candidates. Only this backend
owns IDs, authorization, finalization and publication of generated artifacts.
"""
import hashlib
import secrets
import uuid
from pathlib import Path

from app.db import dump, load, now
from app.errors import ApiError
from app.security import case as require_case
from agent.schemas.public.errors import AccessRevoked, ExecutionRevoked, IntegrityError, JobCancelled
from agent.schemas.public.inputs import SourceRef
from agent.services.hashing import digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS comparison_groups (
 id TEXT PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), name TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, confirmed_json TEXT NOT NULL DEFAULT '{}',
 latest_result_id TEXT, needs_review INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS group_documents (
 group_id TEXT NOT NULL REFERENCES comparison_groups(id), document_id INTEGER NOT NULL REFERENCES documents(id),
 document_version INTEGER NOT NULL, link_version INTEGER NOT NULL,
 PRIMARY KEY(group_id,document_id));
CREATE TABLE IF NOT EXISTS group_confirmations (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES comparison_groups(id), version INTEGER NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_snapshots (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES comparison_groups(id), case_id INTEGER NOT NULL REFERENCES cases(id),
 owner_id INTEGER NOT NULL REFERENCES users(id), input_version INTEGER NOT NULL, group_version INTEGER NOT NULL,
 snapshot_hash TEXT NOT NULL, data_json TEXT NOT NULL, display_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_jobs (
 id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), case_id INTEGER REFERENCES cases(id),
 group_id TEXT REFERENCES comparison_groups(id), document_id INTEGER REFERENCES documents(id),
 snapshot_id TEXT REFERENCES agent_snapshots(id), task_kind TEXT NOT NULL, payload_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 run_id TEXT, execution_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
 error_code TEXT, error_message TEXT, output_json TEXT, result_id TEXT, progress_json TEXT, metadata_json TEXT,
 UNIQUE(owner_id,idempotency_key));
CREATE TABLE IF NOT EXISTS agent_results (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES agent_jobs(id),
 owner_id INTEGER NOT NULL REFERENCES users(id), case_id INTEGER NOT NULL REFERENCES cases(id),
 group_id TEXT NOT NULL REFERENCES comparison_groups(id), snapshot_id TEXT NOT NULL REFERENCES agent_snapshots(id),
 version INTEGER NOT NULL, freshness TEXT NOT NULL, result_hash TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(group_id,version));
CREATE TABLE IF NOT EXISTS document_derivatives (
 id TEXT PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id), document_version INTEGER NOT NULL,
 original_hash TEXT NOT NULL, content_hash TEXT NOT NULL, storage_name TEXT NOT NULL,
 media_type TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(document_id,document_version,original_hash));
CREATE TABLE IF NOT EXISTS document_ocr (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES agent_jobs(id), document_id INTEGER NOT NULL REFERENCES documents(id),
 document_version INTEGER NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extraction_candidates (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES agent_jobs(id), document_id INTEGER NOT NULL REFERENCES documents(id),
 document_version INTEGER NOT NULL, ocr_result_id TEXT NOT NULL REFERENCES document_ocr(id),
 method TEXT NOT NULL, base_candidate_id TEXT, data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_checkpoints (
 job_id TEXT PRIMARY KEY REFERENCES agent_jobs(id), fingerprint TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_artifacts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs(id), owner_id INTEGER NOT NULL REFERENCES users(id),
 run_id TEXT NOT NULL, content_hash TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 storage_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'STAGED', sources_json TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_inquiries (
 id TEXT PRIMARY KEY, result_id TEXT NOT NULL REFERENCES agent_results(id), owner_id INTEGER NOT NULL REFERENCES users(id),
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS agent_jobs_ready ON agent_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS agent_result_history ON agent_results(group_id,version);
CREATE TRIGGER IF NOT EXISTS immutable_agent_snapshot BEFORE UPDATE ON agent_snapshots
BEGIN SELECT RAISE(ABORT,'Input snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_extraction_candidate BEFORE UPDATE ON extraction_candidates
BEGIN SELECT RAISE(ABORT,'Extraction candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_document_reading BEFORE UPDATE ON document_ocr
BEGIN SELECT RAISE(ABORT,'Document readings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_agent_job_input BEFORE UPDATE OF owner_id,case_id,group_id,document_id,snapshot_id,task_kind,payload_json,payload_hash,idempotency_key ON agent_jobs
BEGIN SELECT RAISE(ABORT,'Job inputs are immutable; enqueue a new job'); END;
CREATE TRIGGER IF NOT EXISTS immutable_agent_job_metadata BEFORE UPDATE OF metadata_json ON agent_jobs
BEGIN SELECT RAISE(ABORT,'Job baseline metadata is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_agent_result_content BEFORE UPDATE OF owner_id,case_id,group_id,snapshot_id,result_hash,data_json,version ON agent_results
BEGIN SELECT RAISE(ABORT,'Result content is immutable'); END;
'''


def identifier():
    return uuid.uuid4().hex


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def owned_document(con, document_id, owner_id):
    row = con.execute('''SELECT d.*, w.user_id FROM documents d
        JOIN workplaces w ON w.id=d.workplace_id JOIN users u ON u.id=w.user_id
        WHERE d.id=? AND w.user_id=? AND d.deleted_at IS NULL AND w.deleted_at IS NULL AND u.deleted_at IS NULL''',
        (document_id, owner_id)).fetchone()
    if row is None:
        raise AccessRevoked()
    return row


def source_ref(con, document_id, owner_id, case_id=None):
    doc = owned_document(con, document_id, owner_id)
    derivative = con.execute('''SELECT * FROM document_derivatives WHERE document_id=?
        AND document_version=? AND original_hash=?''', (doc['id'], doc['document_version'], doc['sha256'])).fetchone()
    link_id, link_version, applicable, usage = f'library:{doc["id"]}', 1, None, 'payroll'
    if case_id is not None:
        require_case(con, case_id, owner_id)
        link = con.execute('SELECT * FROM document_links WHERE case_id=? AND document_id=?', (case_id, doc['id'])).fetchone()
        if link is None:
            raise AccessRevoked()
        link_id, link_version = str(link['id']), link['link_version']
        if link['purpose']:
            usage = link['purpose'].lower()
            if usage not in ('payroll','context','both'):
                raise ApiError(422,'INVALID_SOURCE_USAGE',details={'allowed':['payroll','context','both']})
        if link['applicable_from'] and link['applicable_to']:
            applicable = {'start': link['applicable_from'], 'end': link['applicable_to']}
    return SourceRef(source_id=f'doc:{doc["id"]}', owner_id=str(owner_id), workplace_id=str(doc['workplace_id']),
        link_id=link_id, link_version=link_version, document_id=str(doc['id']),
        document_version=doc['document_version'], content_hash=derivative['content_hash'] if derivative else doc['sha256'],
        applicable_period=applicable,usage=usage)


def ensure_default_group(con, case_id):
    gid = f'default-{case_id}'
    con.execute('''INSERT OR IGNORE INTO comparison_groups(id,case_id,name,created_at,updated_at)
        VALUES(?,?,'기본 비교',?,?)''', (gid, case_id, now(), now()))
    return gid


def owned_group(con, group_id, owner_id):
    row = con.execute('SELECT * FROM comparison_groups WHERE id=? AND deleted_at IS NULL', (group_id,)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    require_case(con, row['case_id'], owner_id)
    user = con.execute('SELECT id FROM users WHERE id=? AND deleted_at IS NULL', (owner_id,)).fetchone()
    if user is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def task_sources(payload):
    if 'source' in payload:
        return [SourceRef.model_validate(payload['source'])]
    if 'snapshot' in payload:
        return [SourceRef.model_validate(item) for item in payload['snapshot'].get('authorized_sources', [])]
    return [SourceRef.model_validate(item) for item in payload.get('selected_sources', [])]


def check_job(con, context):
    row = con.execute('SELECT * FROM agent_jobs WHERE id=?', (context.job_id,)).fetchone()
    if row is None or row['status'] != 'RUNNING' or row['run_id'] != context.run_id:
        raise ExecutionRevoked()
    if row['cancel_requested']:
        raise JobCancelled()
    if not secrets.compare_digest(row['execution_hash'] or '', token_hash(context.execution_token.get_secret_value())):
        raise ExecutionRevoked()
    if str(row['owner_id']) != context.owner_id or (str(row['case_id']) if row['case_id'] is not None else None) != context.case_id:
        raise ExecutionRevoked()
    return row


def check_access(con, owner_id, case_id=None, group_id=None, sources=()):
    """Require the current source contract for extraction and analysis work."""
    return _check_access(con, owner_id, case_id, group_id, sources, historical=False)


def check_historical_access(con, owner_id, case_id=None, group_id=None, sources=()):
    """Authorize pinned evidence without treating link metadata edits as deletion.

    The original connection must still exist with the same ID. Document identity,
    version, content hash, ownership and deletion checks remain mandatory.
    """
    return _check_access(con, owner_id, case_id, group_id, sources, historical=True)


def _check_access(con, owner_id, case_id, group_id, sources, *, historical):
    user = con.execute('SELECT id FROM users WHERE id=? AND deleted_at IS NULL', (owner_id,)).fetchone()
    if user is None:
        raise AccessRevoked()
    try:
        if case_id is not None:
            require_case(con, case_id, owner_id)
        if group_id:
            group = owned_group(con, group_id, owner_id)
            if str(group['case_id']) != str(case_id):
                raise AccessRevoked()
    except ApiError as exc:
        raise AccessRevoked() from exc
    for source in sources:
        if source.owner_id != str(owner_id):
            raise AccessRevoked()
        if source.kind == 'manual':
            gid = source.link_id.removeprefix('group:')
            try:
                group = owned_group(con, gid, owner_id)
            except ApiError as exc:
                raise AccessRevoked() from exc
            if str(group['case_id']) != str(case_id):
                raise AccessRevoked()
            # A group's previous confirmation remains readable through its
            # immutable authorized snapshot after an ordinary input edit.
            found = any(source.model_dump(mode='json') in load(row['data_json']).get('authorized_sources', [])
                for row in con.execute('SELECT data_json FROM agent_snapshots WHERE group_id=? AND owner_id=?', (gid, owner_id)))
            if not found:
                raise AccessRevoked()
            continue
        try:
            if historical:
                # Read immutable document identity independently of today's
                # purpose/period. Unlink/relink still changes the connection ID.
                actual = source_ref(con, source.document_id, owner_id)
                if case_id is not None:
                    link = con.execute('SELECT id FROM document_links WHERE case_id=? AND document_id=?',
                        (case_id, source.document_id)).fetchone()
                    if link is None:
                        raise AccessRevoked()
                    actual = actual.model_copy(update={'link_id': str(link['id'])})
                metadata = {'link_version', 'applicable_period', 'usage'}
                matches = actual.model_dump(exclude=metadata) == source.model_dump(exclude=metadata)
            else:
                actual = source_ref(con, source.document_id, owner_id, case_id)
                matches = actual == source
        except ApiError as exc:
            raise AccessRevoked() from exc
        if not matches:
            raise AccessRevoked()


class IntegrationStore:
    def __init__(self, db, data_dir):
        self.db, self.data_dir = db, Path(data_dir)

    def initialize(self):
        for directory in ('uploads', 'derived', 'artifacts'):
            (self.data_dir / directory).mkdir(parents=True, exist_ok=True)
        with self.db.connect(snapshot=False) as con:
            for table, column in (('documents', 'document_version'), ('document_links', 'link_version')):
                if column not in {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1')
            con.executescript(SCHEMA)
            for table, column, definition in (('agent_snapshots','display_json',"TEXT NOT NULL DEFAULT '{}'"),
                                               ('agent_jobs','metadata_json','TEXT')):
                if column not in {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        with self.db.connect(write=True) as con:
            for row in con.execute('SELECT id FROM cases WHERE deleted_at IS NULL').fetchall():
                ensure_default_group(con, row['id'])
        from app.agent_review import initialize_review
        initialize_review(self.db)

    def recover(self):
        """Startup only: revoke old executions, retaining immutable inputs/budgets."""
        with self.db.connect(write=True) as con:
            con.execute("UPDATE agent_jobs SET status=CASE WHEN cancel_requested=1 THEN 'CANCELLED' ELSE 'PENDING' END,execution_hash=NULL,run_id=NULL WHERE status='RUNNING'")
            con.execute("UPDATE documents SET processing_status='UPLOADED' WHERE processing_status='PROCESSING'")
            registered = {
                'derived': {r[0] for r in con.execute('SELECT storage_name FROM document_derivatives')},
                'artifacts': {r[0] for r in con.execute('SELECT storage_name FROM agent_artifacts')},
            }
        # A crash between writing bytes and committing metadata cannot make a
        # private orphan downloadable. Remove these leftovers at single-process
        # startup, after old execution tokens have been revoked.
        for directory,names in registered.items():
            for path in (self.data_dir/directory).iterdir():
                if path.is_file() and path.name not in names and path.suffix in ('.tmp','.pdf','.zip'):
                    path.unlink(missing_ok=True)

    def freeze_snapshot(self, con, group_id, owner_id):
        from app.agent_mapping import snapshot_from_group
        group = owned_group(con, group_id, owner_id)
        snapshot = snapshot_from_group(con, group_id, owner_id, identifier())
        workplace = con.execute('SELECT name FROM workplaces WHERE id=?',(snapshot.workplace_id,)).fetchone()
        names = {source.document_id: con.execute('SELECT file_name FROM documents WHERE id=?',(source.document_id,)).fetchone()[0]
                 for source in snapshot.authorized_sources if source.kind=='document'}
        display = {'workplace_name':workplace['name'],'source_names':names}
        con.execute('''INSERT INTO agent_snapshots(id,group_id,case_id,owner_id,input_version,group_version,snapshot_hash,data_json,display_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', (snapshot.snapshot_id, group_id, group['case_id'], owner_id,
            snapshot.input_version, group['version'], snapshot.snapshot_hash, snapshot.model_dump_json(), dump(display), now()))
        return snapshot

    def check_task_access(self, con, owner_id, case_id, group_id, kind, payload, sources=None):
        refs = task_sources(payload) if sources is None else sources
        if kind not in ('explain_result', 'build_report'):
            return check_access(con, owner_id, case_id, group_id, refs)
        # Only tasks pinned to an accessible, committed result may read its old
        # link metadata. They cannot introduce evidence outside that snapshot.
        result_id = payload.get('result', {}).get('result_id')
        if not result_id:
            raise IntegrityError('Result task requires a committed result')
        _, result_row, snapshot = self.result(con, owner_id, result_id)
        if str(result_row['case_id']) != str(case_id) or result_row['group_id'] != group_id:
            raise AccessRevoked()
        authorized = [SourceRef.model_validate(item) for item in snapshot['authorized_sources']]
        if any(ref not in authorized for ref in refs):
            raise AccessRevoked()
        return check_historical_access(con, owner_id, case_id, group_id, refs)

    def enqueue(self, con, owner_id, kind, payload, *, case_id=None, group_id=None, document_id=None,
                snapshot_id=None, idempotency_key=None, metadata=None):
        payload = payload.model_dump(mode='json') if hasattr(payload, 'model_dump') else payload
        key = idempotency_key or identifier()
        fingerprint = digest({'kind': kind, 'payload': payload, 'case': case_id, 'group': group_id, 'document': document_id,'metadata':metadata})
        previous = con.execute('SELECT * FROM agent_jobs WHERE owner_id=? AND idempotency_key=?', (owner_id, key)).fetchone()
        if previous:
            if previous['payload_hash'] != fingerprint:
                raise ApiError(409, 'IDEMPOTENCY_KEY_REUSED')
            return previous['id']
        self.check_task_access(con, owner_id, case_id, group_id, kind, payload)
        if document_id is not None:
            owned_document(con, document_id, owner_id)
        jid = identifier()
        con.execute('''INSERT INTO agent_jobs(id,owner_id,case_id,group_id,document_id,snapshot_id,task_kind,
            payload_json,payload_hash,idempotency_key,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (jid, owner_id, case_id, group_id, document_id, snapshot_id, kind, dump(payload), fingerprint, key,
             dump(metadata) if metadata is not None else None,now()))
        return jid

    def claim(self, job_id=None):
        with self.db.connect(write=True) as con:
            row = con.execute("SELECT * FROM agent_jobs WHERE status='PENDING' AND cancel_requested=0 " +
                ('AND id=? ' if job_id else '') + 'ORDER BY created_at,id LIMIT 1', (job_id,) if job_id else ()).fetchone()
            if row is None:
                return None
            # Serialize each case / document scope, in addition to the app's
            # configured total concurrency bound.
            busy = con.execute("SELECT id FROM agent_jobs WHERE status='RUNNING' AND ((case_id IS NOT NULL AND case_id=?) OR (document_id IS NOT NULL AND document_id=?))", (row['case_id'], row['document_id'])).fetchone()
            if busy:
                return None
            run_id, token = identifier(), secrets.token_urlsafe(32)
            con.execute("UPDATE agent_jobs SET status='RUNNING',run_id=?,execution_hash=?,attempts=attempts+1,started_at=?,error_code=NULL,error_message=NULL WHERE id=?",
                (run_id, token_hash(token), now(), row['id']))
            value = dict(row)
            value.update(run_id=run_id, token=token, status='RUNNING')
            return value

    def cancel(self, owner_id, job_id):
        with self.db.connect(write=True) as con:
            row = con.execute('SELECT * FROM agent_jobs WHERE id=? AND owner_id=?', (job_id, owner_id)).fetchone()
            if row is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            if row['status'] in ('PENDING', 'RUNNING'):
                con.execute("UPDATE agent_jobs SET cancel_requested=1,status='CANCELLED',execution_hash=NULL,completed_at=? WHERE id=?", (now(), job_id))
                if row['document_id'] is not None:
                    con.execute("UPDATE documents SET processing_status='FAILED',error_code='JOB_CANCELLED',progress=100 WHERE id=? AND deleted_at IS NULL",(row['document_id'],))

    def retry(self, owner_id, job_id):
        with self.db.connect(write=True) as con:
            row = con.execute('SELECT * FROM agent_jobs WHERE id=? AND owner_id=?', (job_id, owner_id)).fetchone()
            if row is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            if row['status'] not in ('FAILED', 'CANCELLED'):
                raise ApiError(409, 'JOB_NOT_RETRYABLE')
            self.check_task_access(con, owner_id, row['case_id'], row['group_id'],
                row['task_kind'], load(row['payload_json']))
            con.execute("UPDATE agent_jobs SET status='PENDING',cancel_requested=0,run_id=NULL,execution_hash=NULL,completed_at=NULL WHERE id=?", (job_id,))
            if row['document_id'] is not None:
                con.execute("UPDATE documents SET processing_status='PROCESSING',error_code=NULL,progress=20 WHERE id=?",(row['document_id'],))
        return job_id

    def job(self, owner_id, job_id):
        with self.db.connect() as con:
            row = con.execute('SELECT * FROM agent_jobs WHERE id=? AND owner_id=?', (job_id, owner_id)).fetchone()
            if row is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            payload = load(row['payload_json'])
            if row['task_kind'] == 'analyze_case' and row['status'] == 'COMPLETED' and row['result_id']:
                check_historical_access(con, owner_id, row['case_id'], row['group_id'], task_sources(payload))
            else:
                self.check_task_access(con, owner_id, row['case_id'], row['group_id'], row['task_kind'], payload,
                    () if row['task_kind'] == 'read_document' else None)
            if row['document_id'] is not None:
                owned_document(con,row['document_id'],owner_id)
            output = load(row['output_json'])
            if row['result_id']:
                result,_,_ = self.result(con,owner_id,row['result_id'])
                output = result.model_dump(mode='json')
            result_id = load(row['payload_json']).get('result',{}).get('result_id')
            if result_id:
                self.result(con,owner_id,result_id)
            return {key: row[key] for key in ('id','task_kind','case_id','group_id','document_id','snapshot_id','status',
                'attempts','created_at','started_at','completed_at','error_code','error_message','result_id')} | {
                    'output': output, 'progress': load(row['progress_json'])}

    def result(self, con, owner_id, result_id):
        from agent.schemas.public.results import AnalysisResult
        row = con.execute('SELECT * FROM agent_results WHERE id=? AND owner_id=?', (result_id, owner_id)).fetchone()
        if row is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        snapshot = con.execute('SELECT * FROM agent_snapshots WHERE id=?', (row['snapshot_id'],)).fetchone()
        sources = [SourceRef.model_validate(item) for item in load(snapshot['data_json'])['authorized_sources']]
        check_historical_access(con, owner_id, row['case_id'], row['group_id'], sources)
        result = AnalysisResult.model_validate_json(row['data_json'])
        group = owned_group(con, row['group_id'], owner_id)
        case = require_case(con, row['case_id'], owner_id)
        if group['version'] != snapshot['group_version'] or case['input_version'] != snapshot['input_version'] or group['needs_review']:
            result.freshness = 'stale'
        return result, row, load(snapshot['data_json'])

    def artifact(self, owner_id, artifact_id):
        with self.db.connect() as con:
            row = con.execute("SELECT a.*,j.case_id,j.group_id,j.payload_json FROM agent_artifacts a JOIN agent_jobs j ON j.id=a.job_id WHERE a.id=? AND a.owner_id=? AND a.status='PUBLISHED' AND j.status='COMPLETED'", (artifact_id, owner_id)).fetchone()
            if row is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            check_historical_access(con, owner_id, row['case_id'], row['group_id'],
                [SourceRef.model_validate(item) for item in load(row['sources_json'])])
            result_id = load(row['payload_json']).get('result', {}).get('result_id')
            if result_id:
                self.result(con, owner_id, result_id)
            path = self.data_dir / 'artifacts' / row['storage_name']
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row['content_hash'] or len(data) != row['size_bytes']:
                raise IntegrityError('Artifact content changed')
            return path, dict(row)
