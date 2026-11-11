"""Review use cases; all slow agent work runs outside database transactions."""

import asyncio
import logging
from copy import deepcopy
from pathlib import Path
from langgraph.errors import NodeCancelledError

from agent.review.model import ReviewModel
from agent.review.pipeline import field_seed, run_pipeline
from agent.review.reading import png_bytes, probe
from agent.review.registry import profiles
from agent.review import state as rules
from agent.services.hashing import content_hash
from app.db import dump, load, now
from app.errors import ApiError
from app.integration_store import identifier
from app.review_store import ReviewStore


class ReviewService:
    def __init__(self, agent_service, model=None):
        self.agent_service = agent_service
        self.db, self.settings = agent_service.db, agent_service.agent_settings
        self.store = ReviewStore(self.db, agent_service.settings.data_dir)
        self.model = model or ReviewModel(self.settings)
        self.model_injected = model is not None

    def require_model(self):
        if not self.model_injected and (not self.settings.openai_api_key or not self.settings.openai_api_key.get_secret_value()):
            raise ApiError(503, 'MODEL_NOT_CONFIGURED')

    def create(self, owner_id, title, auto_process=False, auto_verify=False):
        if auto_process:
            self.require_model()
        rid = identifier()
        state = rules.new_state(title, auto_process, auto_verify)
        with self.db.connect(write=True) as con:
            con.execute('INSERT INTO review_sessions(id,owner_id,state_json,created_at,updated_at) VALUES(?,?,?,?,?)',
                (rid, owner_id, dump(state), now(), now()))
        return self.store.view(owner_id, rid)

    def update(self, owner_id, rid, expected, payload):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            state = load(row['state_json'])
            if payload.get('basic') is not None:
                state['basic'].update(payload['basic'])
                rules.validate_basic(state['basic'])
                state['basic_confirmed'] = False
            if payload.get('options') is not None:
                state['options'].update(payload['options'])
            for key in ('title', 'auto_process', 'auto_verify'):
                if payload.get(key) is not None:
                    state[key] = payload[key]
            if state['auto_process']:
                self.require_model()
            if payload.get('confirm_basic'):
                if not all(state['basic'][key] for key in ('employer_name', 'period_start', 'period_end')):
                    raise ApiError(422, 'REVIEW_BASIC_INFORMATION_REQUIRED')
                state['basic_confirmed'] = True
            self.store.change(con, row, owner_id, 'basic_updated', payload, state)
        return self.store.view(owner_id, rid)

    async def upload(self, owner_id, rid, expected, files):
        metadata = []
        for name, data in files:
            try:
                info = await asyncio.to_thread(probe, data, self.settings)
            except Exception as exc:
                raise ApiError(422, 'INVALID_REVIEW_DOCUMENT') from exc
            metadata.append((Path(name.replace('\\', '/')).name[:240] or 'document', data, info))
        written = []
        try:
            # Write immutable bytes before acquiring the SQLite writer lock.
            for name, data, info in metadata:
                filename = identifier() + info['extension']
                path = self.store.directory / filename
                await asyncio.to_thread(path.write_bytes, data)
                written.append((name, data, info, filename))
            keep = set()
            with self.db.connect(write=True) as con:
                row = self.store.owned(con, owner_id, rid)
                self.store.version(row, expected)
                state = load(row['state_json'])
                if state['auto_process']:
                    self.require_model()
                added = []
                for name, data, info, filename in written:
                    sha = content_hash(data)
                    previous = con.execute('SELECT id FROM review_documents WHERE review_id=? AND sha256=?', (rid, sha)).fetchone()
                    if previous:
                        added.append(previous['id'])
                        continue
                    did = identifier()
                    con.execute('''INSERT INTO review_documents(id,review_id,file_name,storage_name,media_type,
                        sha256,page_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)''',
                        (did, rid, name, filename, info['media_type'], sha, info['pages'], now(), now()))
                    document = self.store.document(con, rid, did)
                    self.store.enqueue(con, document, 'process' if state['auto_process'] else 'read',
                        {'auto_verify': state['auto_verify']})
                    added.append(did)
                    keep.add(filename)
                if keep:
                    self.store.change(con, row, owner_id, 'documents_added', {'document_ids': added})
            for _, _, _, filename in written:
                if filename not in keep:
                    (self.store.directory / filename).unlink(missing_ok=True)
        except BaseException:
            for _, _, _, filename in written:
                (self.store.directory / filename).unlink(missing_ok=True)
            raise
        return self.store.view(owner_id, rid)

    def document_action(self, owner_id, rid, did, expected, action, payload):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            document = self.store.document(con, rid, did)
            state = load(row['state_json'])
            if action in ('exclude', 'include'):
                document['excluded'] = action == 'exclude'
                if action == 'exclude':
                    con.execute("UPDATE review_jobs SET status='CANCELLED',cancel_requested=1,execution_hash=NULL,completed_at=? WHERE document_id=? AND status IN ('PENDING','RUNNING')", (now(), did))
                    document['status'] = 'REVIEW_REQUIRED'
                self.store.save_document(con, document)
            else:
                if document['excluded']:
                    raise ApiError(409, 'REVIEW_DOCUMENT_EXCLUDED')
                if con.execute("SELECT 1 FROM review_jobs WHERE document_id=? AND status IN ('PENDING','RUNNING')", (did,)).fetchone():
                    raise ApiError(409, 'REVIEW_DOCUMENT_BUSY')
                if action == 'manual':
                    parts = rules.validate_parts(payload['parts'], document['page_count'], manual=True)
                    document['parts'] = rules.merge_parts(document['parts'], parts)
                    document['status'] = 'REVIEW_REQUIRED'
                    self.store.save_document(con, document)
                else:
                    self.require_model()
                    options = {'auto_verify': state['auto_verify']}
                    if action == 'classify':
                        options['preset_parts'] = rules.validate_parts(payload['parts'], document['page_count'])
                    elif action == 'verify':
                        ids = payload.get('field_ids') or []
                        if not ids or len(ids) > 50 or not document.get('reading'):
                            raise ApiError(422, 'REVIEW_VERIFICATION_TARGET_REQUIRED')
                        found = [rules.find_field([document], fid)[2] for fid in ids]
                        if any(f['review_state'] == 'confirmed' for f in found):
                            raise ApiError(409, 'REVIEW_FIELD_ALREADY_CONFIRMED')
                        options.update(verify_field_ids=ids, verification_parts=deepcopy(document['parts']))
                    self.store.enqueue(con, document, 'verify' if action == 'verify' else 'process', options)
            self.store.change(con, row, owner_id, 'document_' + action, {'document_id': did, **payload})
        return self.store.view(owner_id, rid)

    def edit(self, owner_id, rid, expected, payload):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            document, part, field = rules.find_field(self.store.documents(con, rid), payload['field_id'])
            previous = deepcopy(field)
            rules.edit(field, part, **{k: v for k, v in payload.items() if k != 'field_id'})
            field['history'].append({'event': 'edited', 'at': now(), 'before': previous | {'history': []}})
            self.store.save_document(con, document)
            self.store.change(con, row, owner_id, 'field_edited', {'field_id': field['id'], 'before': previous, 'after': field})
        return self.store.view(owner_id, rid)

    def add_fields(self, owner_id, rid, expected, part_id, row_type=None, label=None):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            found = [(d, p) for d in self.store.documents(con, rid) if not d['excluded'] for p in d['parts'] if p['id'] == part_id]
            if not found:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            document, part = found[0]
            if row_type:
                spec = profiles()[part['document_type']]['rows'].get(row_type)
                if spec is None:
                    raise ApiError(422, 'INVALID_REVIEW_ROW_TYPE')
                index = 1
                while any(f['field_id'].startswith(f'{row_type}_{index}_') for f in part['fields']):
                    index += 1
                fields = [field_seed(f'{row_type}_{index}_{key}', value) for key, value in spec['fields'].items()]
            else:
                fields = [field_seed('extra_' + identifier(), {'label': label, 'unit': 'text'}, extra=True)]
            for field in fields:
                field.update(touched=True, source_kind='user_statement', issues=['MANUAL'])
            part['fields'].extend(fields)
            self.store.save_document(con, document)
            self.store.change(con, row, owner_id, 'fields_added', {'part_id': part_id, 'field_ids': [f['id'] for f in fields]})
        return self.store.view(owner_id, rid)

    def confirm(self, owner_id, rid, expected, field_ids, source_seen, acknowledgement=None):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            documents = self.store.documents(con, rid)
            fields = [rules.find_field(documents, fid) for fid in dict.fromkeys(field_ids)]
            rules.confirm(fields, source_seen=source_seen, acknowledgement=acknowledgement)
            for document in {d['id']: d for d, _, _ in fields}.values():
                self.store.save_document(con, document)
            self.store.change(con, row, owner_id, 'fields_confirmed', {'field_ids': field_ids,
                'source_seen': source_seen, 'conflict_acknowledgement': acknowledgement})
        return self.store.view(owner_id, rid)

    def control_job(self, owner_id, rid, jid, action, expected):
        with self.db.connect(write=True) as con:
            row = self.store.owned(con, owner_id, rid)
            self.store.version(row, expected)
            job = con.execute('SELECT * FROM review_jobs WHERE id=? AND review_id=?', (jid, rid)).fetchone()
            if job is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            document = self.store.document(con, rid, job['document_id'])
            if action == 'cancel':
                if job['status'] in ('PENDING', 'RUNNING'):
                    con.execute("UPDATE review_jobs SET status='CANCELLED',execution_hash=NULL,cancel_requested=1,completed_at=? WHERE id=?", (now(), jid))
                    con.execute("UPDATE review_documents SET status='REVIEW_REQUIRED' WHERE id=?", (document['id'],))
            else:
                if job['status'] not in ('INTERRUPTED', 'FAILED', 'CANCELLED') or document['excluded']:
                    raise ApiError(409, 'REVIEW_JOB_NOT_RESUMABLE')
                if document['revision'] != job['document_revision']:
                    raise ApiError(409, 'REVIEW_INPUT_CHANGED')
                if con.execute("SELECT 1 FROM review_jobs WHERE document_id=? AND status IN ('PENDING','RUNNING')", (document['id'],)).fetchone():
                    raise ApiError(409, 'REVIEW_DOCUMENT_BUSY')
                if job['kind'] != 'read':
                    self.require_model()
                con.execute("UPDATE review_jobs SET status='PENDING',cancel_requested=0,run_id=NULL,execution_hash=NULL,error_code=NULL WHERE id=?", (jid,))
                con.execute("UPDATE review_documents SET status='PENDING' WHERE id=?", (document['id'],))
            self.store.change(con, row, owner_id, 'job_' + action, {'job_id': jid})
        return self.store.view(owner_id, rid)

    def source(self, owner_id, rid, did):
        with self.db.connect() as con:
            self.store.owned(con, owner_id, rid)
            document = self.store.document(con, rid, did)
        data = (self.store.directory / document['storage_name']).read_bytes()
        if content_hash(data) != document['sha256']:
            raise ApiError(409, 'REVIEW_SOURCE_CHANGED')
        return document, data

    async def page(self, owner_id, rid, did, page):
        document, data = self.source(owner_id, rid, did)
        if not 1 <= page <= document['page_count']:
            raise ApiError(404, 'REVIEW_PAGE_NOT_FOUND')
        image = await asyncio.to_thread(png_bytes, data, document['media_type'], page)
        self.source(owner_id, rid, did)
        return image

    async def execute_next(self, job_id=None):
        async with self.agent_service.semaphore:
            job = self.store.claim(job_id)
            if job is None:
                return False
            await self._execute(job)
            return True

    async def _execute(self, job):
        data = load(job['data_json']) or {}
        payload = load(job['payload_json'])

        def persist():
            with self.db.connect(write=True) as con:
                con.execute('UPDATE review_jobs SET data_json=? WHERE id=? AND run_id=?',
                    (dump(data), job['id'], job['run_id']))

        async def control():
            try:
                with self.db.connect() as con:
                    document = self.store.check(con, job)
                    if document['revision'] != job['document_revision']:
                        raise ApiError(409, 'REVIEW_INPUT_CHANGED')
                original = await asyncio.to_thread((self.store.directory / document['storage_name']).read_bytes)
                if content_hash(original) != job['source_hash']:
                    raise ApiError(409, 'REVIEW_SOURCE_CHANGED')
            except ApiError:
                raise asyncio.CancelledError from None

        async def progress(stage):
            data['stage'] = stage
            await control()
            persist()

        try:
            await control()
            with self.db.connect() as con:
                document = self.store.check(con, job)
            original = await asyncio.to_thread((self.store.directory / document['storage_name']).read_bytes)
            output = await run_pipeline(original, document['media_type'], self.settings, self.model, data, progress,
                read_only=job['kind'] == 'read', auto_verify=payload.get('auto_verify', False),
                prior_reading=document.get('reading') or data.get('reading'),
                preset_parts=payload.get('preset_parts'), verification_parts=payload.get('verification_parts'),
                verify_field_ids=payload.get('verify_field_ids'), persist=persist, control=control)
            await control()
            with self.db.connect(write=True) as con:
                document = self.store.check(con, job)
                if document['revision'] != job['document_revision']:
                    raise ApiError(409, 'REVIEW_INPUT_CHANGED')
                previous = deepcopy(document['parts'])
                targets = set(payload['verify_field_ids']) if job['kind'] == 'verify' else None
                document['parts'] = rules.merge_parts(previous, output['parts'], only_field_ids=targets)
                document['reading'] = output['reading']
                document.setdefault('history', []).append({'job_id': job['id'], 'at': now(), 'parts': previous})
                document['status'] = 'REVIEW_REQUIRED'
                self.store.save_document(con, document)
                row = con.execute('SELECT * FROM review_sessions WHERE id=?', (job['review_id'],)).fetchone()
                self.store.change(con, row, row['owner_id'], 'document_processed', {'job_id': job['id']})
                con.execute("UPDATE review_jobs SET status='COMPLETED',data_json=?,completed_at=?,execution_hash=NULL WHERE id=?",
                    (dump(data), now(), job['id']))
        except (asyncio.CancelledError, NodeCancelledError):
            persist()
            if asyncio.current_task().cancelling():
                raise
            self._fail(job, 'REVIEW_INPUT_OR_ACCESS_CHANGED', 'STALE')
        except Exception as exc:
            logging.getLogger(__name__).exception('review_job_failed job_id=%s', job['id'])
            persist()
            self._fail(job, exc.code if isinstance(exc, ApiError) else 'REVIEW_PROCESSING_FAILED', 'FAILED')

    def _fail(self, job, code, status):
        with self.db.connect(write=True) as con:
            current = con.execute('SELECT * FROM review_jobs WHERE id=?', (job['id'],)).fetchone()
            if current['status'] != 'RUNNING' or current['run_id'] != job['run_id']:
                return
            con.execute('UPDATE review_jobs SET status=?,error_code=?,completed_at=?,execution_hash=NULL WHERE id=?',
                (status, code, now(), job['id']))
            con.execute('UPDATE review_documents SET status=? WHERE id=?', (status, job['document_id']))
