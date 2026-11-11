import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import AIAdapter
from app.errors import ApiError
from app.main import create_app
from tests.backend.conftest import create_case, create_workplace, image_file, manual_payslip, register, upload, wait_for, wait_results


def test_same_net_amount_new_evidence_creates_deposit_snapshot(client, auth, workspace):
    _, cid = workspace
    first = manual_payslip(client, auth, cid)
    before = wait_results(client, cid, auth)
    assert client.delete(f'/api/v1/cases/{cid}/manual-records/{first}', headers=auth).status_code == 204
    second = manual_payslip(client, auth, cid)
    after = wait_results(client, cid, auth)
    assert second != first
    assert after['versions']['depositComparison']['version'] > before['versions']['depositComparison']['version']
    assert after['versions']['depositComparison']['freshness'] == 'CURRENT'


def test_replacing_shared_original_retains_overrides_and_other_case(client, auth, workspace):
    wid, cid = workspace
    other = create_case(client, auth, wid)
    did, _ = upload(client, auth, cid, 'CONTRACT')
    assert client.post(f'/api/v1/cases/{other}/document-links', headers=auth, json={'documentId': did}).status_code == 201
    field = client.get(f'/api/v1/cases/{cid}/documents/{did}/fields', headers=auth).json()['data']['fields'][0]
    assert client.patch(f'/api/v1/cases/{cid}/documents/{did}/fields/{field["fieldId"]}', headers=auth,
                        json={'value': 15000, 'confirmed': True}).status_code == 200
    replaced = client.post(f'/api/v1/cases/{cid}/documents/{did}/replace', headers=auth,
                           files={'file': ('replacement.png', image_file(), 'image/png')})
    assert replaced.status_code == 202, replaced.text
    data = replaced.json()['data']
    rid, new_id = data['replacementId'], data['documentId']
    path = f'/cases/{cid}/document-replacements/{rid}'
    assert data['originalLinkRetained'] is True
    candidate = wait_for(client, path, auth, lambda d: d['candidate']['processingStatus'] in ('READY', 'REVIEW_REQUIRED', 'FAILED'))
    assert candidate['candidate']['processingStatus'] != 'FAILED'
    confirm = client.post('/api/v1' + path + '/confirm', headers=auth, json={'confirmed': True})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()['data']['preservedOverrideCount'] == 1
    values = client.get(f'/api/v1/cases/{cid}/documents/{new_id}/fields', headers=auth).json()['data']['fields']
    assert values[0]['value'] == 15000 and values[0]['hasCaseOverride'] is True
    assert values[0]['source']['carriedFromDocumentId'] == did
    assert values[0]['source']['page'] is None
    assert client.get(f'/api/v1/cases/{cid}/documents/{did}', headers=auth).status_code == 404
    assert client.get(f'/api/v1/cases/{other}/documents/{did}', headers=auth).status_code == 200
    version = confirm.json()['data']['inputVersion']
    assert client.post('/api/v1' + path + '/confirm', headers=auth, json={}).json()['data']['inputVersion'] == version


def test_replacement_blocked_when_override_key_missing(settings):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={'document_id': count, 'document_type': 'employment_contract',
            'fields': [{'key': 'hourlyWage', 'label': '시급', 'value': 10000, 'value_type': 'MONEY_KRW'}] if count == 1 else [],
            'quality_issues': [], 'thread_id': None})
    settings.ai_mode = 'http'
    with TestClient(create_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as client:
        auth = register(client)
        wid = create_workplace(client, auth)
        cid = create_case(client, auth, wid)
        did, _ = upload(client, auth, cid, 'CONTRACT')
        fid = client.get(f'/api/v1/cases/{cid}/documents/{did}/fields', headers=auth).json()['data']['fields'][0]['fieldId']
        client.patch(f'/api/v1/cases/{cid}/documents/{did}/fields/{fid}', headers=auth, json={'confirmed': True})
        data = client.post(f'/api/v1/cases/{cid}/documents/{did}/replace', headers=auth,
            files={'file': ('new.png', image_file(), 'image/png')}).json()['data']
        path = f'/cases/{cid}/document-replacements/{data["replacementId"]}'
        wait_for(client, path, auth, lambda d: d['candidate']['processingStatus'] == 'READY')
        response = client.post('/api/v1' + path + '/confirm', headers=auth, json={})
        assert response.status_code == 409
        assert response.json()['data']['errorCode'] == 'REPLACEMENT_OVERRIDE_CONFLICT'
        assert client.get(f'/api/v1/cases/{cid}/documents/{did}/fields', headers=auth).json()['data']['fields'][0]['hasCaseOverride']


def test_cancel_and_retry_does_not_duplicate_question(settings):
    calls = 0
    cancelled = []
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return httpx.Response(200, json={'content': '재시도 완료', 'sources': []})
    settings.ai_mode = 'http'
    with TestClient(create_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as client:
        auth = register(client)
        wid = create_workplace(client, auth)
        cid = create_case(client, auth, wid)
        chat_id = client.post(f'/api/v1/workplaces/{wid}/chats', headers=auth,
            json={'defaultScope': {'scopeType': 'CASE', 'caseId': cid}}).json()['data']['chatId']
        path = f'/workplaces/{wid}/chats/{chat_id}'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, '/api/v1' + path + '/messages', headers=auth, json={'content': '취소할 질문'})
            history = wait_for(client, path, auth, lambda d: any(m['status'] == 'STREAMING' for m in d['messages']))
            pending = history['messages'][-1]
            cancel_path = '/api/v1' + path + '/runs/' + pending['runId'] + '/cancel'
            assert client.post(cancel_path, headers=auth).json()['data']['status'] == 'CANCELLED'
            stream = future.result(timeout=5)
            assert '"status":"CANCELLED"' in stream.text
        assert cancelled
        retry = client.post('/api/v1' + path + f'/messages/{pending["messageId"]}/retry', headers=auth)
        assert '"status":"COMPLETED"' in retry.text
        messages = client.get('/api/v1' + path, headers=auth).json()['data']['messages']
        assert [m['role'] for m in messages] == ['USER', 'ASSISTANT', 'ASSISTANT']
        assert messages[-1]['retryOfMessageId'] == pending['messageId']
        assert messages[1]['status'] == 'CANCELLED'
        assert client.post('/api/v1' + path + f'/messages/{messages[-1]["messageId"]}/retry', headers=auth).status_code == 409
        assert client.app.state.chat_runs == {}


def test_ai_total_deadline(settings):
    async def handler(request):
        await asyncio.sleep(.2)
        return httpx.Response(200, json={'content': 'too late'})
    async def run():
        settings.ai_mode = 'http'
        settings.ai_timeout_seconds = .02
        adapter = AIAdapter(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(ApiError) as error:
            await adapter.chat({})
        assert error.value.code == 'UPSTREAM_TIMEOUT'
        await adapter.close()
    asyncio.run(run())


def test_restart_repairs_missing_job(settings):
    from app.db import Database
    with TestClient(create_app(settings)) as client:
        auth = register(client)
        wid = create_workplace(client, auth)
        cid = create_case(client, auth, wid)
        manual_payslip(client, auth, cid)
        before = wait_results(client, cid, auth)
    # Simulate an older version failing after the save transaction with no job left to run.
    with Database(settings.db_path).connect(write=True) as con:
        con.execute("UPDATE snapshots SET fingerprint='obsolete-rules' WHERE case_id=?", (cid,))
    with TestClient(create_app(settings)) as restarted:
        after = wait_results(restarted, cid, auth)
        assert after['versions']['payslipValidation']['version'] > before['versions']['payslipValidation']['version']
        assert after['currentInputVersion'] == before['currentInputVersion']


def test_idempotency_transactional_replay_and_payload_conflict(client, auth, workspace):
    wid, cid = workspace
    manual_payslip(client, auth, cid)
    wait_results(client, cid, auth)
    tx = client.post(f'/api/v1/workplaces/{wid}/payment-transactions', headers=auth,
        json={'paymentMethod': 'CASH', 'transactionDate': '2026-11-07', 'amount': 100}).json()['data']['transactionId']
    headers = {**auth, 'Idempotency-Key': 'allocate-once'}
    version = client.get(f'/api/v1/cases/{cid}', headers=auth).json()['data']['inputVersion']
    payload = {'transactionId': tx, 'allocatedAmount': 100, 'expectedInputVersion': version}
    path = f'/api/v1/cases/{cid}/deposits'
    first = client.post(path, headers=headers, json=payload)
    replayed = client.post(path, headers=headers, json=payload)
    assert first.status_code == replayed.status_code == 201
    assert first.json() == replayed.json()
    assert replayed.headers['Idempotency-Replayed'] == 'true'
    assert client.get(path, headers=auth).json()['data']['total'] == 1
    conflict = client.post(path, headers=headers, json={**payload, 'allocatedAmount': 1})
    assert conflict.status_code == 409 and conflict.json()['data']['errorCode'] == 'IDEMPOTENCY_KEY_REUSED'
    wait_results(client, cid, auth)
    report_path = f'/api/v1/cases/{cid}/reports'
    first = client.post(report_path, headers={**auth, 'Idempotency-Key': 'report-once'}, json={})
    replayed = client.post(report_path, headers={**auth, 'Idempotency-Key': 'report-once'}, json={})
    assert first.status_code == replayed.status_code == 202
    assert first.json() == replayed.json()
    assert client.get(report_path, headers=auth).json()['data']['total'] == 1
    assert client.delete(f'/api/v1/cases/{cid}', headers=auth).status_code == 204
    assert client.post(path, headers=headers, json=payload).status_code == 404


def test_existing_database_migration_preserves_relations_and_historical_ids(tmp_path):
    import sqlite3
    from app.db import Database, SCHEMA, dump, now
    path = tmp_path / 'legacy.sqlite3'
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.replace(' PRIMARY KEY AUTOINCREMENT', ' PRIMARY KEY'))
    con.execute("INSERT INTO users(id,email,name,password_hash) VALUES(1,'old@example.com','old','hash')")
    con.execute("INSERT INTO workplaces(id,user_id,name,employment_status,created_at,updated_at) VALUES(1,1,'old','CURRENT',?,?)", (now(), now()))
    con.execute("INSERT INTO cases(id,workplace_id,pay_type,period_start,period_end,updated_at) VALUES(1,1,'HOURLY','2026-10-01','2026-10-31',?)", (now(),))
    con.execute("INSERT INTO manual_records(id,case_id,record_type,data_json,created_at) VALUES(4,1,'PAYSLIP','{}',?)", (now(),))
    con.execute("INSERT INTO snapshots(case_id,kind,version,input_version,fingerprint,data_json,created_at) VALUES(1,'payslipValidation',1,1,'old',?,?)", (dump({'_sources': [{'recordId': 9}]}), now()))
    con.commit()
    con.close()
    db = Database(path)
    db.initialize()
    with db.connect(write=True) as con:
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        con.execute('DELETE FROM manual_records WHERE id=4')
        new = con.execute("INSERT INTO manual_records(case_id,record_type,data_json,created_at) VALUES(1,'PAYSLIP','{}',?)", (now(),)).lastrowid
        assert new > 9
    db.initialize()


def test_password_reset_local_delivery_and_single_use(client, auth, settings):
    from urllib.parse import parse_qs, urlparse
    path = '/api/v1/auth/forgot-password'
    existing = client.post(path, json={'email': 'worker@example.com'})
    missing = client.post(path, json={'email': 'nobody@example.com'})
    assert existing.status_code == missing.status_code == 202
    assert existing.json() == missing.json()
    files = list((settings.data_dir / 'outbox').glob('*.json'))
    assert len(files) == 1
    letter = json.loads(files[0].read_text())
    assert letter['deliveryMode'] == 'LOCAL_OUTBOX_ONLY'
    assert files[0].stat().st_mode & 0o777 == 0o600
    token = parse_qs(urlparse(letter['resetUrl']).query)['token'][0]
    assert token not in existing.text
    response = client.post('/api/v1/auth/reset-password', json={'token': token, 'newPassword': 'newpassword123'})
    assert response.status_code == 204
    assert client.get('/api/v1/users/me', headers=auth).status_code == 401
    assert client.post('/api/v1/auth/refresh').status_code == 401
    assert client.post('/api/v1/auth/reset-password', json={'token': token, 'newPassword': 'anotherpassword'}).status_code == 400
    assert client.post('/api/v1/auth/login', json={'email': 'worker@example.com', 'password': 'password1234'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'email': 'worker@example.com', 'password': 'newpassword123'}).status_code == 200


def test_deletion_status_uses_limited_receipt_cookie(client, auth):
    assert client.get('/api/v1/users/me/deletion').status_code == 401
    deleted = client.request('DELETE', '/api/v1/users/me', headers=auth, json={'confirmation': 'DELETE_ACCOUNT'})
    assert deleted.status_code == 202
    assert client.get('/api/v1/users/me', headers=auth).status_code == 401
    status = client.get('/api/v1/users/me/deletion')
    assert status.status_code == 200
    assert status.json()['data']['deletionRequestId'] == deleted.json()['data']['deletionRequestId']
    assert status.json()['data']['purgeImplemented'] is False
    assert status.json()['data']['normalStoragePurgeDueAt'] is None
    assert 'deletion_receipt=' in deleted.headers['set-cookie'] or 'deletion_receipt' in client.cookies


def test_attendance_comparison_requires_semantics_and_detects_overlap(client, auth, workspace):
    _, cid = workspace
    manual_payslip(client, auth, cid, workedMinutes=960)
    def work(day, start='09', end='18'):
        return client.post(f'/api/v1/cases/{cid}/manual-records', headers=auth, json={'recordType': 'ATTENDANCE', 'data': {
            'workDate': day, 'clockIn': day + f'T{start}:00:00+09:00', 'clockOut': day + f'T{end}:00:00+09:00', 'breakMinutes': 60}})
    assert work('2026-10-14').status_code == 201
    assert work('2026-10-15').status_code == 201
    before = wait_results(client, cid, auth)
    hours = next(i for i in before['items'] if i['id'] == 'worked_minutes')
    assert hours['status'] == 'INSUFFICIENT_INFORMATION'
    assert client.put(f'/api/v1/cases/{cid}/conditions', headers=auth,
        json={'answers': [{'conditionId': 'hours_basis_confirmed', 'value': True}]}).status_code == 200
    after = wait_results(client, cid, auth)
    hours = next(i for i in after['items'] if i['id'] == 'worked_minutes')
    assert hours['status'] == 'MATCH' and hours['calculatedValue'] == 960 and hours['unit'] == 'MINUTE'
    assert work('2026-10-14', '10', '17').status_code == 201
    conflicting = wait_results(client, cid, auth)
    assert next(i for i in conflicting['items'] if i['id'] == 'worked_minutes')['status'] == 'REVIEW_REQUIRED'


def test_base_pay_comparison_uses_confirmed_scope(client, auth, workspace):
    _, cid = workspace
    client.post(f'/api/v1/cases/{cid}/manual-records', headers=auth,
                json={'recordType': 'CONTRACT', 'data': {'hourlyWage': 12000}})
    manual_payslip(client, auth, cid, basePay=240000)
    client.put(f'/api/v1/cases/{cid}/conditions', headers=auth, json={'answers': [
        {'conditionId': 'base_scope_confirmed', 'value': True}, {'conditionId': 'regular_minutes', 'value': 1200},
        {'conditionId': 'base_pay_basis_confirmed', 'value': True}]})
    result = wait_results(client, cid, auth)
    item = next(i for i in result['comparison']['items'] if i['type'] == 'BASE_PAY')
    assert item['status'] == 'MATCH' and item['calculated'] == 240000
    assert result['calculation']['grossPay'] is None
    assert len({i['id'] for i in result['items']}) == len(result['items'])


def test_result_reads_use_consistent_database_snapshot(client, auth, workspace):
    _, cid = workspace
    db = client.app.state.db
    with db.connect() as reader:
        original = reader.execute('SELECT input_version FROM cases WHERE id=?', (cid,)).fetchone()[0]
        with db.connect(write=True) as writer:
            writer.execute('UPDATE cases SET input_version=input_version+1 WHERE id=?', (cid,))
        assert reader.execute('SELECT input_version FROM cases WHERE id=?', (cid,)).fetchone()[0] == original
    with db.connect() as reader:
        assert reader.execute('SELECT input_version FROM cases WHERE id=?', (cid,)).fetchone()[0] == original + 1
