import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.main import create_app
from tests.backend.conftest import create_case, create_workplace, image_file, manual_payslip, register, upload, wait_for, wait_results


def test_upload_confirm_calculate_chat_restart(client, auth, workspace, settings):
    wid, cid = workspace
    did, doc = upload(client, auth, cid)
    assert doc['aiMetadata']['aiMode'] == 'mock'
    assert doc['processingStatus'] == 'REVIEW_REQUIRED'
    path = f'/api/v1/cases/{cid}/documents/{did}'
    fields = client.get(path + '/fields', headers=auth).json()['data']['fields']
    assert all(f['status'] == 'UNCONFIRMED' for f in fields)
    result = wait_results(client, cid, auth)
    from app.outputs import ResultsDTO
    ResultsDTO.model_validate(result)
    assert result['payslipValidation']['paymentTotal']['status'] == 'INSUFFICIENT_INFORMATION'
    for field in fields:
        response = client.patch(path + f'/fields/{field["fieldId"]}', headers=auth, json={'confirmed': True})
        assert response.status_code == 200, response.text
    assert client.post(path + '/review/confirm', headers=auth).json()['data']['reviewStatus'] == 'CONFIRMED'
    result = wait_results(client, cid, auth)
    assert result['payslipValidation']['paymentTotal']['status'] == 'MATCH'
    assert result['payslipValidation']['deductionTotal']['difference'] == -10000
    assert result['payslipValidation']['netPay']['difference'] == 10000
    assert result['calculation']['grossPay'] is None
    assert result['depositComparison']['difference'] is None
    chat = client.post(f'/api/v1/workplaces/{wid}/chats', headers=auth,
                       json={'title': '급여 질문', 'defaultScope': {'scopeType': 'CASE', 'caseId': cid}}).json()['data']['chatId']
    endpoint = f'/api/v1/workplaces/{wid}/chats/{chat}'
    response = client.post(endpoint + '/messages', headers=auth, json={'content': '차이를 설명해 주세요'})
    assert response.status_code == 200
    assert 'event: message_done' in response.text and 'event: content_delta' in response.text
    messages = client.get(endpoint, headers=auth).json()['data']['messages']
    assert len(messages) == 2 and messages[-1]['status'] == 'COMPLETED'
    assert messages[-1]['scope']['caseId'] == cid
    # Separate app reads persisted DB and sessions. No in-memory session dependency.
    with TestClient(create_app(settings)) as restarted:
        saved = restarted.get(f'/api/v1/cases/{cid}/results', headers=auth)
        assert saved.status_code == 200
        assert saved.json()['data']['payslipValidation']['paymentTotal']['status'] == 'MATCH'
        assert len(restarted.get(endpoint, headers=auth).json()['data']['messages']) == 2


def test_shared_defaults_preserve_case_override(client, auth, workspace):
    wid, cid = workspace
    other = create_case(client, auth, wid)
    did, _ = upload(client, auth, cid, 'CONTRACT')
    field = client.get(f'/api/v1/cases/{cid}/documents/{did}/fields', headers=auth).json()['data']['fields'][0]
    fid = field['fieldId']
    assert client.post(f'/api/v1/cases/{other}/document-links', headers=auth, json={'documentId': did}).status_code == 201
    p = f'/api/v1/cases/{other}/documents/{did}'
    assert client.patch(p + f'/fields/{fid}', headers=auth, json={'value': 13000, 'confirmed': True}).status_code == 200
    before = client.get(f'/api/v1/cases/{other}', headers=auth).json()['data']['inputVersion']
    updated = client.patch(f'/api/v1/workplaces/{wid}/documents/{did}/fields/{fid}', headers=auth,
                           json={'value': 14000, 'confirmed': True}).json()['data']
    assert updated['affectedCaseIds'] == [cid]
    assert client.get(p + '/fields', headers=auth).json()['data']['fields'][0]['value'] == 13000
    assert client.get(f'/api/v1/cases/{other}', headers=auth).json()['data']['inputVersion'] == before
    assert len(client.get(p + f'/fields/{fid}/history', headers=auth).json()['data']['items']) == 2
    assert client.delete(p + f'/field-overrides/{fid}', headers=auth).status_code == 200
    assert client.get(p + '/fields', headers=auth).json()['data']['fields'][0]['value'] == 14000


def test_payment_only_changes_deposit_version_and_caps(client, auth, workspace):
    wid, cid = workspace
    manual_payslip(client, auth, cid)
    before = wait_results(client, cid, auth)
    tx = client.post(f'/api/v1/workplaces/{wid}/payment-transactions', headers=auth,
        json={'paymentMethod': 'BANK_TRANSFER', 'transactionDate': '2026-11-07', 'amount': 2000000}).json()['data']['transactionId']
    r = client.post(f'/api/v1/cases/{cid}/deposits', headers=auth, json={'transactionId': tx, 'allocatedAmount': 1950000})
    assert r.status_code == 201, r.text
    after = wait_results(client, cid, auth)
    assert after['versions']['calculation'] == before['versions']['calculation']
    assert after['depositComparison']['difference'] == 140000
    other = create_case(client, auth, wid)
    r = client.post(f'/api/v1/cases/{other}/deposits', headers=auth, json={'transactionId': tx, 'allocatedAmount': 100000})
    assert r.status_code == 409
    assert r.json()['data']['errorCode'] == 'ALLOCATION_EXCEEDS_TRANSACTION'
    duplicate = client.post(f'/api/v1/cases/{cid}/deposits', headers=auth, json={'transactionId': tx, 'allocatedAmount': 1})
    assert duplicate.json()['data']['errorCode'] == 'DUPLICATE_ALLOCATION'
    cash = client.post(f'/api/v1/cases/{cid}/manual-records', headers=auth, json={
        'recordType': 'DEPOSIT', 'data': {'paymentMethod': 'CASH', 'transactionDate': '2026-11-07', 'amount': 140000}})
    assert cash.status_code == 201
    assert cash.json()['data']['recordId'] is None
    after = wait_results(client, cid, auth)
    assert after['depositComparison']['status'] == 'MATCH'
    assert after['depositComparison']['linkedCashPaymentTotal'] == 140000


def test_concurrent_allocations_do_not_exceed_transaction(client, auth, workspace):
    wid, first = workspace
    second = create_case(client, auth, wid)
    tid = client.post(f'/api/v1/workplaces/{wid}/payment-transactions', headers=auth,
        json={'paymentMethod': 'CASH', 'transactionDate': '2026-11-07', 'amount': 100}).json()['data']['transactionId']
    def allocate(cid):
        return client.post(f'/api/v1/cases/{cid}/deposits', headers=auth,
                           json={'transactionId': tid, 'allocatedAmount': 75}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(allocate, [first, second])) == [201, 409]


def test_null_is_not_zero_and_partial_independent_totals(client, auth, workspace):
    _, cid = workspace
    manual_payslip(client, auth, cid, grossPay=0, totalDeductions=0, netPay=0, paymentItems=None, deductionItems=None)
    result = wait_results(client, cid, auth)
    assert result['payslipValidation']['netPay']['status'] == 'MATCH'
    assert result['payslipValidation']['paymentTotal']['status'] == 'INSUFFICIENT_INFORMATION'
    assert result['depositComparison']['linkedPaymentTotal'] is None
    assert result['depositComparison']['status'] == 'INSUFFICIENT_INFORMATION'


def test_versions_conflict_and_superseded_job(client, auth, workspace):
    _, cid = workspace
    version = client.get(f'/api/v1/cases/{cid}', headers=auth).json()['data']['inputVersion']
    body = {'answers': [{'conditionId': 'regular_minutes', 'value': 0}], 'expectedInputVersion': version}
    saved = client.put(f'/api/v1/cases/{cid}/conditions', headers=auth, json=body)
    assert saved.status_code == 200
    stale = client.put(f'/api/v1/cases/{cid}/conditions', headers=auth, json=body)
    assert stale.status_code == 409
    # Supersede queued work with latest input and ensure latest snapshot input wins.
    for minutes in (10, 20):
        assert client.put(f'/api/v1/cases/{cid}/conditions', headers=auth,
            json={'answers': [{'conditionId': 'regular_minutes', 'value': minutes}]}).status_code == 200
    result = wait_results(client, cid, auth)
    assert result['versions']['calculation']['inputVersion'] == result['currentInputVersion']


def test_ownership_scope_and_deletion(client, auth, workspace):
    wid, cid = workspace
    did, _ = upload(client, auth, cid)
    other_auth = register(client, 'other@example.com')
    for path in (f'/cases/{cid}', f'/workplaces/{wid}/documents/{did}/file', f'/cases/{cid}/results'):
        assert client.get('/api/v1' + path, headers=other_auth).status_code == 404
    other_wid = create_workplace(client, auth)
    other_cid = create_case(client, auth, other_wid)
    wrong = client.post(f'/api/v1/workplaces/{wid}/chats', headers=auth,
        json={'defaultScope': {'scopeType': 'CASE', 'caseId': other_cid}})
    assert wrong.status_code == 404
    shared = create_case(client, auth, wid)
    assert client.post(f'/api/v1/cases/{shared}/document-links', headers=auth, json={'documentId': did}).status_code == 201
    assert client.delete(f'/api/v1/cases/{cid}', headers=auth).status_code == 204
    assert client.get(f'/api/v1/cases/{cid}/documents/{did}', headers=auth).status_code == 404
    assert client.get(f'/api/v1/workplaces/{wid}/documents/{did}/file', headers=auth).status_code == 200
    assert client.get(f'/api/v1/cases/{shared}/documents/{did}', headers=auth).status_code == 200


def test_invalid_uploads_and_values(client, auth, workspace):
    _, cid = workspace
    base = f'/api/v1/cases/{cid}'
    fake = client.post(base + '/documents', headers=auth, files={'file': ('fake.pdf', b'not a pdf', 'application/pdf')},
                       data={'documentType': 'CONTRACT'})
    assert fake.status_code == 422
    unsafe = client.post(base + '/documents', headers=auth, files={'file': ('payload.exe', b'x', 'application/octet-stream')},
                         data={'documentType': 'CONTRACT'})
    assert unsafe.status_code == 415
    invalid = client.post(base + '/manual-records', headers=auth,
                          json={'recordType': 'PAYSLIP', 'data': {'grossPay': -1}})
    assert invalid.status_code == 422 and invalid.json()['code'] == 422
    bool_amount = client.post(base + '/manual-records', headers=auth,
                          json={'recordType': 'PAYSLIP', 'data': {'grossPay': True}})
    assert bool_amount.status_code == 422
    times = client.post(base + '/manual-records', headers=auth, json={'recordType': 'ATTENDANCE', 'data': {
        'workDate': '2026-10-03', 'clockIn': '2026-10-03T09:00:00', 'clockOut': '2026-10-03T18:00:00', 'breakMinutes': 60}})
    assert times.status_code == 422


def test_report_frozen_versions_and_selected_zip(client, auth, workspace):
    wid, cid = workspace
    did, _ = upload(client, auth, cid, 'CONTRACT')
    rid = manual_payslip(client, auth, cid)
    before = wait_results(client, cid, auth)
    r = client.post(f'/api/v1/cases/{cid}/reports', headers=auth,
        json={'includeOriginalDocuments': True, 'documentIds': [did]})
    assert r.status_code == 202, r.text
    report_id = r.json()['data']['reportId']
    path = f'/cases/{cid}/reports/{report_id}'
    ready = wait_for(client, path, auth, lambda d: d['status'] in ('READY', 'FAILED'))
    assert ready['status'] == 'READY', ready
    pdf = client.get('/api/v1' + path + '/pdf', headers=auth)
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF-')
    reader = PdfReader(io.BytesIO(pdf.content))
    assert len(reader.pages) >= 1
    assert '월급잇다' in ''.join(p.extract_text() for p in reader.pages)
    archive = client.get('/api/v1' + path + '/originals', headers=auth)
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert len(names) == 2 and 'result-snapshot.json' in names
    assert client.patch(f'/api/v1/cases/{cid}/manual-records/{rid}', headers=auth,
        json={'recordType': 'PAYSLIP', 'data': {'grossPay': 0, 'totalDeductions': 0, 'netPay': 0}}).status_code == 200
    wait_results(client, cid, auth)
    old = client.get('/api/v1' + path, headers=auth).json()['data']
    assert old['isCurrent'] is False
    assert old['referencedSnapshots']['payslipValidationId'] == before['versions']['payslipValidation']['snapshotId']
    assert client.get('/api/v1' + path + '/pdf', headers=auth).content == pdf.content
    assert client.delete(f'/api/v1/workplaces/{wid}/documents/{did}?confirm=true', headers=auth).status_code == 204
    assert client.get('/api/v1' + path + '/pdf', headers=auth).status_code == 404


def test_auth_refresh_logout_account_deletion(client):
    auth = register(client)
    r = client.post('/api/v1/auth/refresh')
    assert r.status_code == 200
    assert client.get('/api/v1/users/me', headers=auth).status_code == 401
    fresh = {'Authorization': 'Bearer ' + r.json()['data']['accessToken']}
    assert client.post('/api/v1/auth/logout', headers=fresh).status_code == 204
    assert client.get('/api/v1/users/me', headers=fresh).status_code == 401
    login = client.post('/api/v1/auth/login', json={'email': 'worker@example.com', 'password': 'password1234'})
    fresh = {'Authorization': 'Bearer ' + login.json()['data']['accessToken']}
    assert client.delete('/api/v1/users/me', headers=fresh).status_code == 422
    r = client.request('DELETE', '/api/v1/users/me', headers=fresh, json={'confirmation': 'DELETE_ACCOUNT'})
    assert r.status_code == 202
    assert client.get('/api/v1/users/me', headers=fresh).status_code == 401


def test_cors_and_openapi(client):
    r = client.options('/api/v1/workplaces', headers={'Origin': 'http://localhost:3000',
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
    assert r.status_code == 200
    assert r.headers['access-control-allow-origin'] == 'http://localhost:3000'
    schema = client.get('/openapi.json').json()
    assert '/api/v1/cases/{case_id}/documents' in schema['paths']
    assert client.get('/docs').status_code == 200


def test_base_arithmetic_hourly_and_monthly(client, auth, workspace):
    wid, cid = workspace
    assert client.post(f'/api/v1/cases/{cid}/manual-records', headers=auth,
        json={'recordType': 'CONTRACT', 'data': {'hourlyWage': 10001}}).status_code == 201
    assert client.put(f'/api/v1/cases/{cid}/conditions', headers=auth,
        json={'answers': [{'conditionId': 'base_scope_confirmed', 'value': True},
                          {'conditionId': 'regular_minutes', 'value': 1}]}).status_code == 200
    result = wait_results(client, cid, auth)
    assert result['calculation']['calculatedSubtotal'] == 167
    assert result['calculation']['grossPay'] is None
    assert all(i['amount'] is None for i in result['calculation']['items'][1:])
    other = create_case(client, auth, wid)
    assert client.patch(f'/api/v1/cases/{other}', headers=auth, json={'payType': 'MONTHLY'}).status_code == 200
    assert client.post(f'/api/v1/cases/{other}/manual-records', headers=auth,
        json={'recordType': 'CONTRACT', 'data': {'monthlyBasePay': 2000000}}).status_code == 201
    assert client.put(f'/api/v1/cases/{other}/conditions', headers=auth,
        json={'answers': [{'conditionId': 'base_scope_confirmed', 'value': True}]}).status_code == 200
    assert wait_results(client, other, auth)['calculation']['calculatedSubtotal'] == 2000000


def test_consultation_nationwide_fallback(client):
    response = client.get('/api/v1/consultation-centers?region=청주시&category=WAGE').json()['data']
    assert response['nationwideFallbackUsed'] is True
    assert response['items'][0]['sourceUrl'].startswith('https://1350.moel.go.kr/')
    assert response['items'][0]['verifiedAt'] == '2026-11-08'
    assert response['items'][0]['languages'] is None
