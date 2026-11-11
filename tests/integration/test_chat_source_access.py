"""Chat permission regressions; the model is always an in-process recording stub."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, load, now
from app.main import create_app
from app.routers import chats
from app.schemas import MessageCreate


class RecordingModel:
    def __init__(self):
        self.calls = []

    async def chat(self, payload):
        self.calls.append(deepcopy(payload))
        return {'content': '확인한 자료에 대한 답변입니다. ' * 8, 'sources': []}


@pytest.fixture
def chat_api(tmp_path):
    model = RecordingModel()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path/'data', workers_enabled=False),
                     ai_client=model)
    with TestClient(app) as client:
        signup = client.post('/api/v1/auth/signup', json={
            'email': 'chat-review@example.test', 'password': 'secure-test-password', 'name': '테스트 사용자'})
        assert signup.status_code == 201, signup.text
        token = client.post('/api/v1/auth/login', json={
            'email': 'chat-review@example.test', 'password': 'secure-test-password'}).json()['data']['accessToken']
        client.headers['Authorization'] = 'Bearer ' + token
        wid = client.post('/api/v1/workplaces', json={'name': '테스트 사업장'}).json()['data']['workplaceId']
        cid = client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'HOURLY',
            'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}).json()['data']['caseId']
        with app.state.db.connect(write=True) as con:
            uid = con.execute('SELECT user_id FROM workplaces WHERE id=?', (wid,)).fetchone()[0]
            did = con.execute('''INSERT INTO documents(workplace_id,document_type,file_name,storage_name,
                media_type,sha256,page_count,created_at) VALUES(?,'PAYSLIP','synthetic.pdf','unused.pdf',
                'application/pdf','synthetic-hash',1,?)''', (wid, now())).lastrowid
            lid = con.execute('INSERT INTO document_links(case_id,document_id) VALUES(?,?)', (cid, did)).lastrowid
        chat_id = client.post(f'/api/v1/workplaces/{wid}/chats', json={
            'defaultScope': {'scopeType': 'CASE', 'caseId': cid}}).json()['data']['chatId']
        yield SimpleNamespace(client=client, app=app, model=model, wid=wid, cid=cid,
                              uid=uid, did=did, lid=lid, chat_id=chat_id)


def chat_path(h):
    return f'/api/v1/workplaces/{h.wid}/chats/{h.chat_id}'


def scope_for(h, scope_type):
    return {'scopeType': 'CASE', 'caseId': h.cid} if scope_type == 'CASE' else {'scopeType': 'WORKPLACE_ALL'}


def send(h, content='급여 자료를 설명해 주세요.', scope_type='CASE'):
    response = h.client.post(chat_path(h) + '/messages', json={
        'content': content, 'scope': scope_for(h, scope_type)})
    assert response.status_code == 200, response.text
    assert '"status":"COMPLETED"' in response.text, response.text
    return response


@pytest.mark.parametrize('scope_type', ['CASE', 'WORKPLACE_ALL'])
def test_unlink_and_relink_never_restore_chat_or_model_history(chat_api, scope_type):
    h = chat_api
    send(h, scope_type=scope_type)
    original = h.client.get(chat_path(h)).json()['data']['messages']
    original_ids = {message['messageId'] for message in original}
    assert len(original_ids) == 2
    assert original[-1]['sourceLinks'] == [{'caseId': h.cid, 'documentId': h.did, 'linkId': h.lid}]
    # An ordinary update changes link_version, but not the grant identity.
    assert h.client.patch(f'/api/v1/cases/{h.cid}/document-links/{h.lid}', json={'purpose': 'both'}).status_code == 200
    assert len(h.client.get(chat_path(h)).json()['data']['messages']) == 2
    assert h.client.delete(f'/api/v1/cases/{h.cid}/document-links/{h.lid}').status_code == 204
    assert h.client.get(chat_path(h)).json()['data']['messages'] == []
    send(h, '해제 후 새 질문', scope_type)
    assert h.model.calls[-1]['history'] == []
    assert not any(c['documents'] for c in h.model.calls[-1]['context'])
    relink = h.client.post(f'/api/v1/cases/{h.cid}/document-links', json={'documentId': h.did})
    assert relink.status_code == 201, relink.text
    assert relink.json()['data']['linkId'] != h.lid
    send(h, '재연결 후 새 질문', scope_type)
    visible_ids = {m['messageId'] for m in h.client.get(chat_path(h)).json()['data']['messages']}
    assert not original_ids & visible_ids
    assert not any(m['content'] == '급여 자료를 설명해 주세요.' for m in h.model.calls[-1]['history'])


def test_workplace_scope_retains_each_case_connection(chat_api):
    h = chat_api
    cid2 = h.client.post('/api/v1/cases', json={'workplaceId': h.wid, 'payType': 'HOURLY',
        'periodStart': '2026-09-01', 'periodEnd': '2026-09-30'}).json()['data']['caseId']
    lid2 = h.client.post(f'/api/v1/cases/{cid2}/document-links', json={
        'documentId': h.did}).json()['data']['linkId']
    send(h, scope_type='WORKPLACE_ALL')
    messages = h.client.get(chat_path(h)).json()['data']['messages']
    assert {s['linkId'] for s in messages[-1]['sourceLinks']} == {h.lid, lid2}
    assert h.client.delete(f'/api/v1/cases/{h.cid}/document-links/{h.lid}').status_code == 204
    # A surviving link in another period must not revive the revoked context.
    assert h.client.get(chat_path(h)).json()['data']['messages'] == []
    send(h, scope_type='WORKPLACE_ALL')
    assert h.model.calls[-1]['history'] == []


def test_history_provenance_is_carried_into_new_messages(chat_api, monkeypatch):
    h = chat_api
    send(h)
    real_context = chats.build_context

    def context_without_documents(con, workplace_id, scope):
        context = real_context(con, workplace_id, scope)
        for item in context:
            item['documents'] = []
        return context

    # The model still receives prior messages even if a later context is narrower.
    monkeypatch.setattr(chats, 'build_context', context_without_documents)
    send(h, '앞선 답변을 다시 설명해 주세요.')
    assert h.model.calls[-1]['history']
    messages = h.client.get(chat_path(h)).json()['data']['messages']
    assert messages[-1]['sourceLinks'] == messages[0]['sourceLinks']
    assert messages[-2]['sourceLinks'] == messages[0]['sourceLinks']
    assert h.client.delete(f'/api/v1/cases/{h.cid}/document-links/{h.lid}').status_code == 204
    assert h.client.get(chat_path(h)).json()['data']['messages'] == []


@pytest.mark.parametrize('scope_type', ['CASE', 'WORKPLACE_ALL'])
def test_legacy_messages_keep_only_reconstructable_unchanged_access(chat_api, scope_type):
    h = chat_api
    scope = scope_for(h, scope_type)
    with h.app.state.db.connect(write=True) as con:
        metadata = {'inputVersions': chats.scope_versions(con, scope, h.wid), 'documentIds': [h.did]}
        con.execute('''INSERT INTO messages(chat_id,role,content,scope_json,status,metadata_json,created_at)
            VALUES(?,'ASSISTANT','기존 확인 자료',?,'COMPLETED',?,?)''',
            (h.chat_id, dump(scope), dump(metadata), now()))
        con.execute('''INSERT INTO messages(chat_id,role,content,scope_json,status,metadata_json,created_at)
            VALUES(?,'ASSISTANT','출처를 복원할 수 없는 자료',?,'COMPLETED','{}',?)''',
            (h.chat_id, dump(scope), now()))
    visible = h.client.get(chat_path(h)).json()['data']['messages']
    assert [m['content'] for m in visible] == ['기존 확인 자료']
    send(h, scope_type=scope_type)
    assert [m['content'] for m in h.model.calls[-1]['history']] == ['기존 확인 자료']
    assert h.client.delete(f'/api/v1/cases/{h.cid}/document-links/{h.lid}').status_code == 204
    assert h.client.post(f'/api/v1/cases/{h.cid}/document-links', json={'documentId': h.did}).status_code == 201
    assert h.client.get(chat_path(h)).json()['data']['messages'] == []
    send(h, scope_type=scope_type)
    assert h.model.calls[-1]['history'] == []


class LocalRequest:
    def __init__(self, app):
        self.app = app

    async def is_disconnected(self):
        return False


def revoke_link(h):
    with h.app.state.db.connect(write=True) as con:
        con.execute('DELETE FROM document_links WHERE id=?', (h.lid,))
        con.execute('UPDATE cases SET input_version=input_version+1 WHERE id=?', (h.cid,))


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before_stream', 'before_model', 'model_running', 'between_chunks', 'before_done'])
async def test_stream_rechecks_sources_at_every_transmission_boundary(chat_api, phase):
    h = chat_api
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def held_model(payload):
        calls.append(payload)
        started.set()
        if phase == 'model_running':
            await release.wait()
        return SimpleNamespace(content='문서 비공개 내용 ' * 16, sources=[], thread_id=None)

    h.app.state.ai.chat = held_model
    response = await chats.start_message(h.wid, h.chat_id,
        MessageCreate(content='자료 확인', scope=scope_for(h, 'CASE')),
        LocalRequest(h.app), {'id': h.uid})
    stream = response.body_iterator
    emitted = []
    if phase == 'before_stream':
        revoke_link(h)
    else:
        emitted.append(await anext(stream))
        assert 'event: message_start' in emitted[-1]
        if phase == 'before_model':
            revoke_link(h)
        elif phase == 'model_running':
            next_chunk = asyncio.create_task(anext(stream))
            await asyncio.wait_for(started.wait(), timeout=2)
            revoke_link(h)
            release.set()
            emitted.append(await asyncio.wait_for(next_chunk, timeout=2))
        elif phase == 'between_chunks':
            emitted.append(await anext(stream))
            assert 'event: content_delta' in emitted[-1]
            revoke_link(h)
        else:
            while 'event: sources' not in emitted[-1]:
                emitted.append(await anext(stream))
            revoke_link(h)
    emitted.extend([chunk async for chunk in stream])
    text = ''.join(emitted)
    assert 'RESOURCE_NOT_FOUND' in text
    assert '"status":"COMPLETED"' not in text
    if phase in ('before_stream', 'before_model'):
        assert calls == []
    if phase in ('before_stream', 'before_model', 'model_running'):
        assert 'event: content_delta' not in text
    if phase == 'between_chunks':
        assert text.count('event: content_delta') == 1
    with h.app.state.db.connect() as con:
        rows = con.execute('SELECT * FROM messages WHERE chat_id=?', (h.chat_id,)).fetchall()
        assert all(chats.message_dto(con, row, h.wid) is None for row in rows)
        assert all(load(row['metadata_json'])['sourceLinks'][0]['linkId'] == h.lid for row in rows)
