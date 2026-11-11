"""SQLite/file AgentPorts with user authorization and fenced writes."""
import asyncio

from app.db import dump, load, now
from app.integration_store import check_job, owned_document, task_sources
from agent.schemas.public.errors import IntegrityError, OCRRequired
from agent.schemas.public.ports import CheckpointEnvelope, SourceContent
from agent.schemas.public.results import ArtifactRef
from agent.services.hashing import content_hash, digest


class DatabaseAgentPorts:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def _check(self, con, context, sources=None):
        row = check_job(con, context)
        payload = load(row['payload_json'])
        refs = task_sources(payload) if sources is None else sources
        self.store.check_task_access(con, context.owner_id, context.case_id, row['group_id'],
            row['task_kind'], payload, refs)
        return row

    async def check_control(self, context):
        with self.db.connect() as con:
            check_job(con, context)

    async def check_access(self, context, sources):
        with self.db.connect() as con:
            self._check(con, context, sources)

    async def read_source(self, context, source):
        with self.db.connect() as con:
            self._check(con, context, [source])
            doc = owned_document(con, source.document_id, context.owner_id)
            derivative = con.execute('''SELECT * FROM document_derivatives WHERE document_id=? AND document_version=?
                AND original_hash=? AND content_hash=?''', (doc['id'], source.document_version, doc['sha256'], source.content_hash)).fetchone()
            if derivative:
                path = self.store.data_dir / 'derived' / derivative['storage_name']
                mime = derivative['media_type']
            else:
                path = self.store.data_dir / 'uploads' / doc['storage_name']
                mime = doc['media_type']
        data = await asyncio.to_thread(path.read_bytes)
        await self.check_access(context, [source])
        if content_hash(data) != source.content_hash:
            raise IntegrityError('Source content hash changed')
        return SourceContent(data=data, mime_type=mime, document_version=source.document_version,
            content_hash=source.content_hash)

    async def read_ocr(self, context, source, ocr_result_id):
        with self.db.connect() as con:
            self._check(con, context, [source])
            row = con.execute('SELECT * FROM document_ocr WHERE id=?', (ocr_result_id,)).fetchone()
            if row is None:
                raise OCRRequired()
            if (str(row['document_id']) != source.document_id or row['document_version'] != source.document_version
                or row['content_hash'] != source.content_hash or row['source_id'] != source.source_id):
                raise IntegrityError('Stored reading belongs to a different source')
            return load(row['data_json'])

    async def read_original(self, context, source):
        """Return the uploaded bytes, retaining image/PDF derivative lineage.

        The public SourceRef identifies the PDF used by extraction. Reports
        explicitly request the associated uploaded original through this port.
        """
        with self.db.connect() as con:
            self._check(con,context,[source])
            doc = owned_document(con,source.document_id,context.owner_id)
            path = self.store.data_dir/'uploads'/doc['storage_name']
            expected_hash,mime,version = doc['sha256'],doc['media_type'],doc['document_version']
        data = await asyncio.to_thread(path.read_bytes)
        await self.check_access(context,[source])
        if content_hash(data)!=expected_hash:
            raise IntegrityError('Uploaded original content hash changed')
        return SourceContent(data=data,mime_type=mime,document_version=version,content_hash=expected_hash)

    async def load_checkpoint(self, context, fingerprint):
        with self.db.connect() as con:
            self._check(con, context)
            row = con.execute('SELECT * FROM agent_checkpoints WHERE job_id=?', (context.job_id,)).fetchone()
            if row is None:
                return None
            if row['fingerprint'] != fingerprint:
                raise IntegrityError('Same job cannot change input or configuration')
            return CheckpointEnvelope.model_validate_json(row['data_json'])

    async def save_checkpoint(self, context, checkpoint):
        with self.db.connect(write=True) as con:
            self._check(con, context)
            if checkpoint.job_id != context.job_id:
                raise IntegrityError('Checkpoint belongs to another job')
            previous = con.execute('SELECT * FROM agent_checkpoints WHERE job_id=?', (context.job_id,)).fetchone()
            if previous:
                old = CheckpointEnvelope.model_validate_json(previous['data_json'])
                if old.input_fingerprint != checkpoint.input_fingerprint or any(
                    checkpoint.call_budget.get(key, 0) < value for key, value in old.call_budget.items()):
                    raise IntegrityError('Checkpoint fingerprint/budget cannot decrease')
            con.execute('''INSERT INTO agent_checkpoints(job_id,fingerprint,data_json,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at''',
                (context.job_id, checkpoint.input_fingerprint, checkpoint.model_dump_json(), now()))

    async def stage_artifact(self, context, data, mime_type):
        await self.check_access(context, [])
        if mime_type not in ('application/pdf', 'application/zip'):
            raise IntegrityError('Unsupported generated artifact type')
        hashed = content_hash(data)
        aid = digest([context.job_id, hashed, mime_type])
        suffix = '.pdf' if mime_type == 'application/pdf' else '.zip'
        storage_name = aid + suffix
        path = self.store.data_dir / 'artifacts' / storage_name
        temp = path.with_suffix('.' + context.run_id + '.tmp')
        await asyncio.to_thread(temp.write_bytes, data)
        existed = path.exists()
        moved = False
        try:
            with self.db.connect(write=True) as con:
                row = self._check(con, context)
                refs = task_sources(load(row['payload_json']))
                # All bytes were written before opening the transaction. The
                # same-volume rename is short; unregistered bytes stay private.
                temp.replace(path)
                moved = True
                con.execute('''INSERT INTO agent_artifacts(id,job_id,owner_id,run_id,content_hash,mime_type,size_bytes,
                    storage_name,sources_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,status='STAGED' ''',
                    (aid, context.job_id, context.owner_id, context.run_id, hashed, mime_type, len(data), storage_name,
                    dump([ref.model_dump(mode='json') for ref in refs]), now()))
        except BaseException:
            if moved and not existed:
                path.unlink(missing_ok=True)
            raise
        finally:
            temp.unlink(missing_ok=True)
        return ArtifactRef(artifact_id=aid, content_hash=hashed, mime_type=mime_type, size_bytes=len(data))

    async def emit_progress(self, context, event):
        with self.db.connect(write=True) as con:
            check_job(con, context)
            con.execute('UPDATE agent_jobs SET progress_json=? WHERE id=?', (event.model_dump_json(), context.job_id))


# The public name describes the deployment role rather than a local test store.
AgentPorts = DatabaseAgentPorts
