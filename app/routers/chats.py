import asyncio
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.db import dump, load, now
from app.engine import effective_fields, results
from app.errors import ApiError, ok
from app.routers.workspaces import page_data
from app.schemas import ChatCreate, ChatPatch, MessageCreate
from app.security import case, current_user, workplace

router = APIRouter(tags=['AI Chat'])
SOURCE_ACCESS_VERSION = 1


@dataclass
class RunControl:
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


def chat(con, workplace_id, chat_id):
    row = con.execute('SELECT * FROM chats WHERE id=? AND workplace_id=?', (chat_id, workplace_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def validate_scope(con, workplace_id, scope, user_id):
    if scope['scopeType'] == 'CASE':
        c = case(con, scope['caseId'], user_id)
        if c['workplace_id'] != workplace_id:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')


def chat_dto(row):
    return {'chatId': row['id'], 'workplaceId': row['workplace_id'], 'title': row['title'],
            'defaultScope': load(row['default_scope']), 'createdAt': row['created_at']}


def scope_versions(con, scope, workplace_id):
    if scope['scopeType'] == 'CASE':
        ids = [scope['caseId']]
    else:
        ids = [r[0] for r in con.execute('SELECT id FROM cases WHERE workplace_id=? AND deleted_at IS NULL ORDER BY id', (workplace_id,))]
    versions = {}
    for cid in ids:
        row = con.execute('SELECT input_version FROM cases WHERE id=? AND deleted_at IS NULL', (cid,)).fetchone()
        if row:
            versions[str(cid)] = row[0]
    return versions


def source_links_accessible(con, scope, workplace_id, metadata):
    links = metadata.get('sourceLinks')
    document_ids = metadata.get('documentIds')
    if metadata.get('sourceAccessVersion') != SOURCE_ACCESS_VERSION or not isinstance(links, list):
        return False
    if not isinstance(document_ids, list) or any(type(did) is not int for did in document_ids):
        return False
    seen_documents = set()
    for source in links:
        if (not isinstance(source, dict) or set(source) != {'caseId', 'documentId', 'linkId'}
                or any(type(value) is not int or value <= 0 for value in source.values())):
            return False
        if scope['scopeType'] == 'CASE' and source['caseId'] != scope['caseId']:
            return False
        row = con.execute('''SELECT l.id FROM document_links l
            JOIN cases c ON c.id=l.case_id JOIN documents d ON d.id=l.document_id
            JOIN workplaces w ON w.id=c.workplace_id JOIN users u ON u.id=w.user_id
            WHERE l.id=? AND l.case_id=? AND l.document_id=? AND c.workplace_id=?
            AND d.workplace_id=c.workplace_id AND c.deleted_at IS NULL
            AND d.deleted_at IS NULL AND w.deleted_at IS NULL AND u.deleted_at IS NULL''',
            (source['linkId'], source['caseId'], source['documentId'], workplace_id)).fetchone()
        if row is None:
            return False
        seen_documents.add(source['documentId'])
    return seen_documents == set(document_ids)


def accessible_source_metadata(con, scope, workplace_id, metadata, versions):
    if 'sourceAccessVersion' not in metadata:
        # Legacy messages can retain access only while their exact case inputs
        # are unchanged. Every unlink/relink/replacement increments that version,
        # so changed or incomplete legacy provenance is never reconstructed.
        if ('inputVersions' not in metadata or metadata['inputVersions'] != versions
                or not isinstance(metadata.get('documentIds'), list)
                or any(type(did) is not int or did <= 0 for did in metadata['documentIds'])):
            return None
        links = []
        for did in metadata['documentIds']:
            rows = con.execute('''SELECT l.id,l.case_id FROM document_links l
                JOIN cases c ON c.id=l.case_id WHERE l.document_id=? AND c.workplace_id=?
                AND c.deleted_at IS NULL ORDER BY l.id''', (did, workplace_id)).fetchall()
            links.extend({'caseId': row['case_id'], 'documentId': did, 'linkId': row['id']}
                         for row in rows if str(row['case_id']) in versions)
        metadata = {**metadata, 'sourceAccessVersion': SOURCE_ACCESS_VERSION, 'sourceLinks': links}
    return metadata if source_links_accessible(con, scope, workplace_id, metadata) else None


def merge_source_links(*collections):
    links = {(item['caseId'], item['documentId'], item['linkId']): item
             for collection in collections for item in collection}
    return [links[key] for key in sorted(links)]


def message_dto(con, row, workplace_id):
    scope = load(row['scope_json'])
    meta = load(row['metadata_json']) or {}
    versions = scope_versions(con, scope, workplace_id)
    meta = accessible_source_metadata(con, scope, workplace_id, meta, versions)
    if meta is None:
        return None
    # Hide deleted-case messages even in Workplace chat history.
    if scope['scopeType'] == 'CASE' and str(scope['caseId']) not in versions:
        return None
    if scope['scopeType'] == 'WORKPLACE_ALL' and set(meta.get('inputVersions', {})) - set(versions):
        return None
    return {'messageId': row['id'], 'role': row['role'], 'content': row['content'], 'scope': scope,
            'status': row['status'], 'runId': row['run_id'], 'createdAt': row['created_at'], **meta,
            'sourceMayBeOutdated': meta.get('inputVersions', {}) != versions}


@router.post('/workplaces/{workplace_id}/chats', status_code=201)
def create_chat(workplace_id: int, body: ChatCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        scope = body.defaultScope.model_dump(exclude_none=True)
        validate_scope(con, workplace_id, scope, user['id'])
        cid = con.execute('INSERT INTO chats(workplace_id,title,default_scope,created_at) VALUES(?,?,?,?)',
                          (workplace_id, body.title, dump(scope), now())).lastrowid
        return ok(chat_dto(chat(con, workplace_id, cid)), 201)


@router.get('/workplaces/{workplace_id}/chats')
def list_chats(workplace_id: int, request: Request, page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        return ok(page_data([chat_dto(r) for r in con.execute('SELECT * FROM chats WHERE workplace_id=? ORDER BY id DESC', (workplace_id,))], page, size))


@router.get('/workplaces/{workplace_id}/chats/{chat_id}')
def get_chat(workplace_id: int, chat_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        row = chat(con, workplace_id, chat_id)
        messages = [message_dto(con, r, workplace_id) for r in con.execute('SELECT * FROM messages WHERE chat_id=? ORDER BY id', (chat_id,))]
        return ok({**chat_dto(row), 'messages': [m for m in messages if m is not None]})


@router.patch('/workplaces/{workplace_id}/chats/{chat_id}')
def rename_chat(workplace_id: int, chat_id: int, body: ChatPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        chat(con, workplace_id, chat_id)
        con.execute('UPDATE chats SET title=? WHERE id=?', (body.title, chat_id))
        return ok(chat_dto(chat(con, workplace_id, chat_id)))


@router.delete('/workplaces/{workplace_id}/chats/{chat_id}', status_code=204)
def delete_chat(workplace_id: int, chat_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        chat(con, workplace_id, chat_id)
        if con.execute("SELECT 1 FROM messages WHERE chat_id=? AND status='STREAMING'", (chat_id,)).fetchone():
            raise ApiError(409, 'CHAT_RUN_IN_PROGRESS')
        con.execute('DELETE FROM messages WHERE chat_id=?', (chat_id,))
        con.execute('DELETE FROM chats WHERE id=?', (chat_id,))
    return Response(status_code=204)


def build_context(con, workplace_id, scope):
    case_ids = [scope['caseId']] if scope['scopeType'] == 'CASE' else [r[0] for r in con.execute(
        'SELECT id FROM cases WHERE workplace_id=? AND deleted_at IS NULL ORDER BY id', (workplace_id,))]
    contexts = []
    for cid in case_ids:
        docs = []
        for row in con.execute('''SELECT d.*,l.id AS connection_id FROM documents d JOIN document_links l ON l.document_id=d.id
            WHERE l.case_id=? AND d.deleted_at IS NULL ORDER BY d.id''', (cid,)):
            docs.append({'document_id': row['id'], 'document_type': row['document_type'], 'page_count': row['page_count'],
                         'link_id': row['connection_id'],
                         'fields': effective_fields(con, cid, row['id'])})
        contexts.append({'case_id': cid, 'results': results(con, cid), 'documents': docs})
    return contexts


def event(name, payload):
    return f'event: {name}\ndata: {dump(payload)}\n\n'


@router.post('/workplaces/{workplace_id}/chats/{chat_id}/messages', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
async def send_message(workplace_id: int, chat_id: int, body: MessageCreate, request: Request, user=Depends(current_user)):
    return await start_message(workplace_id, chat_id, body, request, user)


@router.post('/workplaces/{workplace_id}/chats/{chat_id}/messages/{message_id}/retry', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
async def retry_message(workplace_id: int, chat_id: int, message_id: int, request: Request, user=Depends(current_user)):
    return await start_message(workplace_id, chat_id, None, request, user, retry_of=message_id)


@router.post('/workplaces/{workplace_id}/chats/{chat_id}/runs/{run_id}/cancel', status_code=202)
async def cancel_run(workplace_id: int, chat_id: int, run_id: str, request: Request, user=Depends(current_user)):
    state = request.app.state
    with state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        chat(con, workplace_id, chat_id)
        message = con.execute('SELECT * FROM messages WHERE chat_id=? AND run_id=?', (chat_id, run_id)).fetchone()
        if message is None or message_dto(con, message, workplace_id) is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        if message['status'] == 'STREAMING':
            con.execute("UPDATE messages SET status='CANCELLED' WHERE id=?", (message['id'],))
            control = state.chat_runs.get(run_id)
            if control:
                control.cancelled.set()
            status = 'CANCELLED'
        else:
            status = message['status']
    return ok({'runId': run_id, 'messageId': message['id'], 'status': status}, 202)


async def start_message(workplace_id, chat_id, body, request, user, retry_of=None):
    state = request.app.state
    with state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        session = chat(con, workplace_id, chat_id)
        retry_user = None
        if retry_of is not None:
            previous = con.execute('SELECT * FROM messages WHERE id=? AND chat_id=?', (retry_of, chat_id)).fetchone()
            if previous is None or message_dto(con, previous, workplace_id) is None:
                raise ApiError(404, 'RESOURCE_NOT_FOUND')
            if previous['role'] != 'ASSISTANT' or previous['status'] not in ('FAILED', 'CANCELLED'):
                raise ApiError(409, 'MESSAGE_NOT_RETRYABLE')
            meta = load(previous['metadata_json']) or {}
            if meta.get('userMessageId'):
                retry_user = con.execute("SELECT * FROM messages WHERE id=? AND chat_id=? AND role='USER'",
                                         (meta['userMessageId'], chat_id)).fetchone()
            else:
                retry_user = con.execute("SELECT * FROM messages WHERE chat_id=? AND id<? AND role='USER' AND scope_json=? ORDER BY id DESC LIMIT 1",
                                         (chat_id, retry_of, previous['scope_json'])).fetchone()
            if retry_user is None or message_dto(con, retry_user, workplace_id) is None:
                raise ApiError(409, 'MESSAGE_NOT_RETRYABLE')
            body = MessageCreate(content=retry_user['content'], scope=load(previous['scope_json']))
        scope = body.scope.model_dump(exclude_none=True) if body.scope else load(session['default_scope'])
        validate_scope(con, workplace_id, scope, user['id'])
        if con.execute("SELECT 1 FROM messages WHERE chat_id=? AND status='STREAMING'", (chat_id,)).fetchone():
            raise ApiError(409, 'CHAT_RUN_IN_PROGRESS')
        context = build_context(con, workplace_id, scope)
        input_versions = scope_versions(con, scope, workplace_id)
        # Strict per-scope history prevents previously broad messages leaking into a CASE query.
        history_rows = []
        for previous in con.execute('''SELECT * FROM messages WHERE chat_id=? AND scope_json=? AND status='COMPLETED' ORDER BY id''', (chat_id, dump(scope))):
            visible = message_dto(con, previous, workplace_id)
            if (retry_user is None or previous['id'] != retry_user['id']) and visible is not None:
                history_rows.append(visible)
        history_rows = history_rows[-20:]
        history = [{'role': previous['role'], 'content': previous['content']} for previous in history_rows]
        context_links = [{'caseId': c['case_id'], 'documentId': d['document_id'], 'linkId': d['link_id']}
                         for c in context for d in c['documents']]
        inherited_links = [previous['sourceLinks'] for previous in history_rows]
        if retry_user is not None:
            inherited_links.append(message_dto(con, retry_user, workplace_id)['sourceLinks'])
        source_links = merge_source_links(context_links, *inherited_links)
        metadata = {'inputVersions': input_versions, 'aiMode': state.settings.ai_mode,
                    'sourceAccessVersion': SOURCE_ACCESS_VERSION, 'sourceLinks': source_links,
                    'documentIds': sorted({source['documentId'] for source in source_links})}
        if retry_user is None:
            user_message_id = con.execute('INSERT INTO messages(chat_id,role,content,scope_json,status,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',
                    (chat_id, 'USER', body.content, dump(scope), 'COMPLETED', dump(metadata), now())).lastrowid
        else:
            user_message_id = retry_user['id']
            metadata['retryOfMessageId'] = retry_of
        metadata['userMessageId'] = user_message_id
        run_id = 'run-' + uuid.uuid4().hex
        message_id = con.execute('INSERT INTO messages(chat_id,role,content,scope_json,status,run_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (chat_id, 'ASSISTANT', '', dump(scope), 'STREAMING', run_id, dump(metadata), now())).lastrowid
    payload = {'thread_id': f'chat-{chat_id}-' + ('case-' + str(scope['caseId']) if scope['scopeType'] == 'CASE' else 'workplace-all'),
               'workplace_id': workplace_id, 'chat_id': chat_id, 'scope': scope, 'content': body.content,
               'history': history, 'context': context}
    control = RunControl()
    state.chat_runs[run_id] = control

    def live_access(con):
        workplace(con, workplace_id, user['id'])
        validate_scope(con, workplace_id, scope, user['id'])
        current_versions = scope_versions(con, scope, workplace_id)
        if set(input_versions) - set(current_versions):
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        if not source_links_accessible(con, scope, workplace_id, metadata):
            raise ApiError(404, 'RESOURCE_NOT_FOUND')

    async def call_model():
        # The stream and task may start after the request transaction commits.
        # Recheck at actual execution, immediately before external transmission.
        with state.db.connect() as con:
            live_access(con)
        return await state.ai.chat(payload)

    async def stream():
        completed = False
        cancelled_wait = None
        partial = ''
        try:
            with state.db.connect() as con:
                live_access(con)
            yield event('message_start', {'runId': run_id, 'messageId': message_id, 'role': 'ASSISTANT', 'scope': scope,
                                          'aiMode': state.settings.ai_mode})
            # Proposed upstream is bounded JSON; downstream SSE emits deltas after response validation.
            control.task = asyncio.create_task(call_model())
            cancelled_wait = asyncio.create_task(control.cancelled.wait())
            while not control.task.done():
                done, _ = await asyncio.wait([control.task, cancelled_wait], timeout=2, return_when=asyncio.FIRST_COMPLETED)
                if control.cancelled.is_set():
                    break
                if not done:
                    if await request.is_disconnected():
                        raise asyncio.CancelledError()
                    with state.db.connect() as con:
                        live_access(con)
                    yield ': waiting\n\n'
            if control.cancelled.is_set():
                yield event('message_done', {'runId': run_id, 'messageId': message_id, 'status': 'CANCELLED'})
                return
            answer = control.task.result()
            permitted = {(c['case_id'], d['document_id']): d for c in context for d in c['documents']}
            sources = []
            for source in answer.sources:
                doc = permitted.get((source.case_id, source.document_id))
                if (doc is None or (source.field_id is not None and source.field_id not in {f['fieldId'] for f in doc['fields']})
                        or (source.page is not None and source.page > doc['page_count'])):
                    raise ApiError(502, 'UPSTREAM_SOURCE_OUT_OF_SCOPE')
                sources.append({'caseId': source.case_id, 'documentId': source.document_id,
                                'page': source.page, 'fieldId': source.field_id})
            for start in range(0, len(answer.content), 48):
                if control.cancelled.is_set():
                    yield event('message_done', {'runId': run_id, 'messageId': message_id, 'status': 'CANCELLED'})
                    return
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                # Recheck live access before each chunk; Case deletion revokes this stream too.
                with state.db.connect() as con:
                    live_access(con)
                delta = answer.content[start:start+48]
                partial += delta
                yield event('content_delta', {'delta': delta})
                await asyncio.sleep(0)
            refs = [{'caseId': c['case_id'], 'type': kind, 'snapshotId': ref['snapshotId'], 'version': ref['version']}
                    for c in context for kind, ref in c['results']['versions'].items() if ref['snapshotId'] is not None]
            metadata.update(sources=sources, resultRefs=refs, upstreamThreadId=answer.thread_id)
            with state.db.connect(write=True) as con:
                live_access(con)
                updated = con.execute("UPDATE messages SET content=?,status='COMPLETED',metadata_json=? WHERE id=? AND status='STREAMING'",
                                      (answer.content, dump(metadata), message_id)).rowcount
            if not updated:
                yield event('message_done', {'runId': run_id, 'messageId': message_id, 'status': 'CANCELLED'})
                return
            completed = True
            with state.db.connect() as con:
                live_access(con)
            yield event('sources', {'sources': sources, 'resultRefs': refs})
            with state.db.connect() as con:
                live_access(con)
            yield event('message_done', {'runId': run_id, 'messageId': message_id, 'status': 'COMPLETED'})
        except ApiError as exc:
            metadata['errorCode'] = exc.code
            yield event('error', {'runId': run_id, 'messageId': message_id, 'errorCode': exc.code, 'retryable': exc.status >= 500})
        except asyncio.CancelledError:
            metadata['errorCode'] = 'RUN_CANCELLED' if control.cancelled.is_set() else 'CLIENT_DISCONNECTED'
            raise
        except Exception:
            metadata['errorCode'] = 'CHAT_GENERATION_FAILED'
            yield event('error', {'runId': run_id, 'messageId': message_id, 'errorCode': 'CHAT_GENERATION_FAILED', 'retryable': True})
        finally:
            pending = [task for task in (control.task, cancelled_wait) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not completed:
                if control.cancelled.is_set():
                    metadata['errorCode'] = 'RUN_CANCELLED'
                with state.db.connect(write=True) as con:
                    con.execute("UPDATE messages SET content=?,metadata_json=?,status=CASE WHEN status='CANCELLED' THEN 'CANCELLED' ELSE 'FAILED' END WHERE id=? AND status IN ('STREAMING','CANCELLED')", (partial, dump(metadata), message_id))
            state.chat_runs.pop(run_id, None)
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
