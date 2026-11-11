"""Real DB, HTTP and engine acceptance for the first review integration phase."""

import asyncio
import io
import threading
import zipfile

import pytest
from langgraph.errors import NodeCancelledError
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from app.config import Settings
from app.db import load
from app.main import create_app

DATA = {
    "contract": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "pay_type": ("monthly", "category"),
        "contract_rate": ("2300000", "KRW/month"),
        "contract_start": ("2026-01-01", "date"),
    },
    "payslip": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "period_start": ("2026-10-01", "date"),
        "period_end": ("2026-10-31", "date"),
        "gross": ("2300000", "KRW"),
        "deduction": ("300000", "KRW"),
        "net": ("2000000", "KRW"),
        "earning_1_label": ("Base salary", "text"),
        "earning_1_amount": ("2300000", "KRW"),
    },
    "deposit": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "account_holder": ("Alex", "text"),
        "transaction_1_date": ("2026-11-10", "date"),
        "transaction_1_description": ("Salary", "text"),
        "transaction_1_deposit_amount": ("2000000", "KRW"),
    },
}


def sample_pdf(kinds=("payslip",), *, rotate=0):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, invariant=1)
    for kind in kinds:
        y = 780
        canvas.drawString(50, y, "TYPE " + kind)
        for key, (value, _) in DATA[kind].items():
            y -= 25
            canvas.drawString(50, y, key + ": " + value)
        canvas.showPage()
    canvas.save()
    if rotate:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(buffer.getvalue()))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page.rotate(rotate))
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    return buffer.getvalue()


class ReviewFixtureModel:
    def __init__(self):
        self.calls = []
        self.pause = None

    async def invoke(self, operation, data):
        self.calls.append(operation)
        if self.pause:
            await self.pause.wait()
        if operation == "classify":
            parts = []
            for page in data["pages"]:
                kind = next((kind for kind in DATA if "TYPE " + kind in page["text"]), "payslip")
                parts.append(
                    dict(
                        document_type=kind,
                        start_page=page["page"],
                        end_page=page["page"],
                        label=kind,
                        needs_review=False,
                        reason="",
                    )
                )
            return {"parts": parts}, {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}
        kind = data["document_type"]
        page = data.get("pages", data.get("images", [{"page": 1}]))[0]["page"]
        targets = data.get("target_field_ids")
        fields = []
        for key, (value, unit) in DATA[kind].items():
            if targets is not None and key not in targets:
                continue
            fields.append(
                dict(
                    field_id=key,
                    raw_value=value,
                    normalized_value=value,
                    unit=unit,
                    page=page,
                    quote=key + ": " + value,
                    issues=[],
                )
            )
        return {
            "document_type": kind,
            "fields": fields,
            "tables": [],
            "additional_fields": [],
            "issues": [],
        }, {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}


def login(client, email='review@example.com'):
    response = client.post('/api/v1/auth/signup', json={'email': email, 'password': 'review-password-123', 'name': 'Review user'})
    assert response.status_code == 201, response.text
    token = client.post('/api/v1/auth/login', json={'email': email, 'password': 'review-password-123'}).json()['data']['accessToken']
    client.headers['Authorization'] = 'Bearer ' + token
    return token


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()['data']


@pytest.fixture
def review_api(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / 'data', workers_enabled=False)
    model = ReviewFixtureModel()
    app = create_app(settings, review_model=model)
    with TestClient(app) as client:
        login(client)
        yield client, app, model, settings


def create(client, **kwargs):
    return checked(client.post('/api/v1/reviews', json=kwargs), 201)


def upload(client, review, content=None, name='sample.pdf'):
    return checked(client.post('/api/v1/reviews/' + review['id'] + '/documents',
        data={'revision': review['revision']}, files=[('files', (name, content or sample_pdf(), 'application/octet-stream'))]), 202)


def current(client, review):
    return checked(client.get('/api/v1/reviews/' + review['id']))


def run(client, app, review):
    for job in review['jobs']:
        if job['status'] == 'PENDING':
            client.portal.call(app.state.reviews.execute_next, job['id'])
    return current(client, review)


def all_fields(review):
    return [f for d in review['documents'] for p in d['parts'] for f in p['fields']]


def prepare(client, review):
    review = checked(client.put('/api/v1/reviews/' + review['id'], json={'revision': review['revision'],
        'basic': {'employer_name': 'Cafe ABC', 'pay_type': 'MONTHLY', 'period_start': '2026-10-01', 'period_end': '2026-10-31'},
        'options': {'subjects_confirmed': True}, 'confirm_basic': True}))
    ids = [f['id'] for f in all_fields(review) if f['value'] and f['validation_status'] == 'valid']
    return checked(client.post('/api/v1/reviews/' + review['id'] + '/confirm',
        json={'revision': review['revision'], 'field_ids': ids, 'source_seen': True}))


def complete(client, review, **kwargs):
    return checked(client.post('/api/v1/reviews/' + review['id'] + '/complete',
        json={'revision': review['revision'], **kwargs}), 202)


def test_real_http_processing_coordinates_confirmation_engine_and_idempotent_export(review_api):
    client, app, model, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True)))
    assert model.calls == ['classify', 'extract']
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    assert gross['value'] == '2300000' and gross['review_state'] == 'unreviewed'
    assert gross['evidence']['location_status'] == 'located'
    assert gross['evidence']['boxes']
    document = review['documents'][0]
    page = client.get(f"/api/v1/reviews/{review['id']}/documents/{document['id']}/pages/1.png")
    assert page.status_code == 200 and page.content.startswith(b'\x89PNG')
    assert client.get(f"/api/v1/reviews/{review['id']}/documents/{document['id']}/pages/2.png").status_code == 404
    blocked = client.post('/api/v1/reviews/' + review['id'] + '/complete', json={'revision': review['revision']})
    assert blocked.status_code == 409
    review = prepare(client, review)
    assert review['readiness']['status'] == 'ready', review['readiness']
    exported = complete(client, review)
    assert complete(client, review)['id'] == exported['id']
    assert client.post('/api/v1/reviews/' + review['id'] + '/complete',
        json={'revision': review['revision'], 'analyze': False}).status_code == 409
    client.portal.call(app.state.service.execute_job, exported['job_id'])
    job = checked(client.get('/api/v1/agent-jobs/' + exported['job_id']))
    assert job['status'] == 'COMPLETED', job
    result = job['output']
    assert next(r for r in result['comparisons'] if r['rule_id'] == 'R-05')['status'] == 'match'
    assert result['wage_section']['gross_total'] is None
    assert next(f for f in result['evidence_catalog'] if f['page'] == 1)['verification_method'] == 'manual_confirmed'
    group = checked(client.get('/api/v1/comparison-groups/' + exported['group_id']))
    assert group['latestResultId'] == job['result_id']
    assert len(model.calls) == 2


def test_mixed_pdf_exports_disjoint_parts_and_existing_contract_is_preserved(review_api):
    client, app, _, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True), sample_pdf(('contract', 'payslip'))))
    assert len(review['documents'][0]['parts']) == 2
    review = prepare(client, review)
    exported = complete(client, review)
    group = checked(client.get('/api/v1/comparison-groups/' + exported['group_id']))
    assert len(group['documentIds']) == 2
    response = client.put('/api/v1/comparison-groups/' + exported['group_id'] + '/documents',
        json={'expectedVersion': group['version'], 'documentIds': group['documentIds']})
    assert response.status_code == 200, response.text
    assert client.post('/api/v1/documents/' + str(group['documentIds'][0]) + '/extract').status_code == 409
    contract = checked(client.get('/api/v1/agent-contracts'))
    assert '1.1' in str(contract)


def test_duplicate_upload_and_revision_ownership_authentication(review_api):
    client, app, model, _ = review_api
    review = upload(client, create(client, auto_process=True))
    duplicate = upload(client, review)
    assert duplicate['revision'] == review['revision'] and len(duplicate['jobs']) == 1
    assert model.calls == []
    assert client.put('/api/v1/reviews/' + review['id'], json={'revision': 1, 'title': 'stale'}).status_code == 409
    assert client.put('/api/v1/reviews/' + review['id'], json={'revision': True, 'title': 'bad'}).status_code == 422
    token = client.headers['Authorization']
    login(client, 'other-review@example.com')
    did, jid = review['documents'][0]['id'], review['jobs'][0]['id']
    for suffix in ('', '/history', '/jobs/' + jid, '/documents/' + did + '/original', '/documents/' + did + '/pages/1.png'):
        assert client.get('/api/v1/reviews/' + review['id'] + suffix).status_code == 404
    client.headers.pop('Authorization')
    assert client.get('/api/v1/reviews').status_code == 401
    client.headers['Authorization'] = token
    bad = client.post('/api/v1/reviews/' + review['id'] + '/documents', data={'revision': review['revision']},
        files=[('files', ('invalid.pdf', b'not a PDF', 'application/pdf'))])
    assert bad.status_code == 422
    review = run(client, app, review)
    assert len(model.calls) == 2


def test_explicit_image_verification_is_bounded_and_never_confirms(review_api):
    client, app, model, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True)))
    field = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    did = review['documents'][0]['id']
    review = checked(client.post(f"/api/v1/reviews/{review['id']}/documents/{did}", json={
        'revision': review['revision'], 'action': 'verify', 'field_ids': [field['id']]}))
    review = run(client, app, review)
    field = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    assert model.calls == ['classify', 'extract', 'verify']
    assert field['review_state'] == 'unreviewed' and len(field['candidates']) == 2
    assert review['jobs'][-1]['usage']['total_tokens'] == 10


def test_manual_png_review_without_model_preserves_original_in_report_zip(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / 'data', workers_enabled=False))
    with TestClient(app) as client:
        login(client)
        assert client.post('/api/v1/reviews', json={'auto_process': True}).status_code == 503
        image = io.BytesIO()
        Image.new('RGB', (120, 80), 'white').save(image, format='PNG')
        original = image.getvalue()
        review = upload(client, create(client), original, 'original.png')
        jid, did = review['jobs'][0]['id'], review['documents'][0]['id']
        review = checked(client.post(f"/api/v1/reviews/{review['id']}/jobs/{jid}", json={
            'revision': review['revision'], 'action': 'cancel'}))
        review = checked(client.post(f"/api/v1/reviews/{review['id']}/documents/{did}", json={
            'revision': review['revision'], 'action': 'manual',
            'parts': [{'document_type': 'payslip', 'start_page': 1, 'end_page': 1}]}))
        for key, value in (('gross', '2300000'), ('deduction', '300000'), ('net', '2000000')):
            fid = next(f['id'] for f in all_fields(review) if f['field_id'] == key)
            review = checked(client.put('/api/v1/reviews/' + review['id'] + '/fields',
                json={'revision': review['revision'], 'field_id': fid, 'value': value}))
        # Manual values still require explicit field confirmation.
        review = checked(client.put('/api/v1/reviews/' + review['id'], json={'revision': review['revision'],
            'basic': {'employer_name': 'Cafe ABC', 'pay_type': 'MONTHLY', 'period_start': '2026-10-01', 'period_end': '2026-10-31'},
            'options': {'subjects_confirmed': True}, 'confirm_basic': True}))
        review = checked(client.post('/api/v1/reviews/' + review['id'] + '/confirm', json={
            'revision': review['revision'], 'field_ids': [f['id'] for f in all_fields(review) if f['value']], 'source_seen': True}))
        exported = complete(client, review)
        client.portal.call(app.state.service.execute_job, exported['job_id'])
        result = checked(client.get('/api/v1/agent-jobs/' + exported['job_id']))
        assert result['status'] == 'COMPLETED', result
        rid = result['result_id']
        expl = checked(client.post('/api/v1/agent-results/' + rid + '/explanations'), 202)['jobId']
        client.portal.call(app.state.service.execute_job, expl)
        doc_ids = checked(client.get('/api/v1/comparison-groups/' + exported['group_id']))['documentIds']
        report = checked(client.post('/api/v1/agent-results/' + rid + '/reports', json={
            'explanationJobId': expl, 'documentIds': doc_ids}), 202)['jobId']
        client.portal.call(app.state.service.execute_job, report)
        report_job = checked(client.get('/api/v1/agent-jobs/' + report))
        assert report_job['status'] == 'COMPLETED', report_job
        archive = next(a for a in report_job['output']['artifacts'] if a['mime_type'] == 'application/zip')
        download = client.get('/api/v1/artifacts/' + archive['artifact_id'])
        assert download.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download.content)) as zipped:
            filename = next(n for n in zipped.namelist() if n.endswith('.png'))
            assert zipped.read(filename) == original
            assert 'source_manifest.json' in zipped.namelist()


def test_conflicting_visual_candidate_needs_individual_source_acknowledgement(review_api):
    client, app, model, _ = review_api
    original_invoke = model.invoke

    async def conflicting(operation, data):
        response, usage = await original_invoke(operation, data)
        if operation == 'verify':
            response['fields'][0].update(raw_value='2400000', normalized_value='2400000', quote='gross: 2400000')
        return response, usage

    model.invoke = conflicting
    review = run(client, app, upload(client, create(client, auto_process=True)))
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    did = review['documents'][0]['id']
    review = run(client, app, checked(client.post(f"/api/v1/reviews/{review['id']}/documents/{did}",
        json={'revision': review['revision'], 'action': 'verify', 'field_ids': [gross['id']]})))
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    assert gross['conflict_id']
    assert client.post('/api/v1/reviews/' + review['id'] + '/confirm', json={
        'revision': review['revision'], 'field_ids': [gross['id']], 'source_seen': True}).status_code == 422
    review = checked(client.put('/api/v1/reviews/' + review['id'] + '/fields', json={
        'revision': review['revision'], 'field_id': gross['id'], 'candidate_id': gross['candidates'][-1]['id']}))
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    net = next(f for f in all_fields(review) if f['field_id'] == 'net')
    assert client.post('/api/v1/reviews/' + review['id'] + '/confirm', json={
        'revision': review['revision'], 'field_ids': [gross['id'], net['id']], 'source_seen': True,
        'conflict_acknowledgement': gross['conflict_id']}).status_code == 422
    review = checked(client.post('/api/v1/reviews/' + review['id'] + '/confirm', json={
        'revision': review['revision'], 'field_ids': [gross['id']], 'source_seen': True,
        'conflict_acknowledgement': gross['conflict_id']}))
    assert next(f for f in all_fields(review) if f['field_id'] == 'gross')['review_state'] == 'confirmed'


@pytest.mark.parametrize('mutation', ['cancel', 'delete', 'tamper', 'account'])
def test_inflight_revocation_or_tampering_never_publishes_candidates(review_api, mutation):
    client, app, model, settings = review_api
    entered, release = threading.Event(), threading.Event()
    original_invoke = model.invoke

    async def blocked(operation, data):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await original_invoke(operation, data)

    model.invoke = blocked
    review = upload(client, create(client, auto_process=True))
    jid, did = review['jobs'][0]['id'], review['documents'][0]['id']
    future = client.portal.start_task_soon(app.state.reviews.execute_next, jid)
    assert entered.wait(5)
    if mutation == 'cancel':
        checked(client.post(f"/api/v1/reviews/{review['id']}/jobs/{jid}", json={
            'revision': review['revision'], 'action': 'cancel'}))
    elif mutation == 'delete':
        assert client.delete('/api/v1/reviews/' + review['id'], params={'revision': review['revision']}).status_code == 204
    elif mutation == 'account':
        with app.state.db.connect(write=True) as con:
            con.execute("UPDATE users SET deleted_at='2026-11-11' WHERE id=1")
    else:
        with app.state.db.connect() as con:
            filename = con.execute('SELECT storage_name FROM review_documents WHERE id=?', (did,)).fetchone()[0]
        (settings.data_dir / 'review_uploads' / filename).write_bytes(b'changed')
    release.set()
    future.result(timeout=10)
    with app.state.db.connect() as con:
        saved = con.execute('SELECT * FROM review_jobs WHERE id=?', (jid,)).fetchone()
        assert saved['status'] in ('CANCELLED', 'STALE')
        assert not load(con.execute('SELECT data_json FROM review_documents WHERE id=?', (did,)).fetchone()[0]).get('parts')
        assert con.execute('SELECT COUNT(*) FROM extraction_candidates').fetchone()[0] == 0


def test_user_edit_during_reprocessing_is_preserved_and_old_job_is_stale(review_api):
    client, app, model, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True)))
    did = review['documents'][0]['id']
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    review = checked(client.post(f"/api/v1/reviews/{review['id']}/documents/{did}",
        json={'revision': review['revision'], 'action': 'process'}))
    entered, release = threading.Event(), threading.Event()
    original_invoke = model.invoke

    async def blocked(operation, data):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await original_invoke(operation, data)

    model.invoke = blocked
    jid = review['jobs'][-1]['id']
    future = client.portal.start_task_soon(app.state.reviews.execute_next, jid)
    assert entered.wait(5)
    review = checked(client.put('/api/v1/reviews/' + review['id'] + '/fields', json={
        'revision': review['revision'], 'field_id': gross['id'], 'value': '2200000'}))
    release.set()
    future.result(timeout=10)
    review = current(client, review)
    assert review['jobs'][-1]['status'] == 'STALE'
    assert next(f for f in all_fields(review) if f['field_id'] == 'gross')['value'] == '2200000'
    assert '진행 중' in ' '.join(review['readiness']['missing'])


def test_restart_recovers_drafts_and_resumes_cached_classification_without_new_call(review_api):
    client, app, model, settings = review_api
    review = upload(client, create(client, auto_process=True))
    jid = review['jobs'][0]['id']
    claimed = app.state.reviews.store.claim(jid)
    assert claimed is not None
    # Save one successfully completed model step, as after a process interruption.
    from agent.review.pipeline import run_pipeline
    captured = {}

    async def stop_after_classification(operation, data):
        if operation == 'extract':
            raise asyncio.CancelledError
        return await model.invoke(operation, data)

    model.invoke_original = model.invoke
    class InterruptedModel:
        invoke = staticmethod(stop_after_classification)

    async def pipeline():
        async def progress(stage):
            pass
        await run_pipeline(sample_pdf(), 'application/pdf', app.state.agent_settings,
            InterruptedModel(), captured, progress, auto_verify=False)

    with pytest.raises((asyncio.CancelledError, NodeCancelledError)):
        client.portal.call(pipeline)
    from app.db import dump
    with app.state.db.connect(write=True) as con:
        con.execute('UPDATE review_jobs SET data_json=? WHERE id=?', (dump(captured), jid))
    fresh_model = ReviewFixtureModel()
    replacement = create_app(settings, review_model=fresh_model)
    with TestClient(replacement) as next_client:
        next_client.headers['Authorization'] = client.headers['Authorization']
        review = current(next_client, review)
        assert review['jobs'][0]['status'] == 'INTERRUPTED'
        review = checked(next_client.post(f"/api/v1/reviews/{review['id']}/jobs/{jid}",
            json={'revision': review['revision'], 'action': 'resume'}))
        review = run(next_client, replacement, review)
        assert review['jobs'][0]['status'] == 'COMPLETED'
        assert fresh_model.calls == ['extract']
        assert review['jobs'][0]['usage']['total_tokens'] == 20


def test_pending_manual_clear_and_reprocessing_preserve_user_deletion(review_api):
    client, app, _, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True)))
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    review = checked(client.put('/api/v1/reviews/' + review['id'] + '/fields', json={
        'revision': review['revision'], 'field_id': gross['id'], 'value': ''}))
    did = review['documents'][0]['id']
    review = checked(client.post(f"/api/v1/reviews/{review['id']}/documents/{did}",
        json={'revision': review['revision'], 'action': 'process'}))
    review = run(client, app, review)
    gross = next(f for f in all_fields(review) if f['field_id'] == 'gross')
    assert gross['value'] == '' and gross['touched'] and gross['review_state'] == 'unreviewed'
    assert len(gross['candidates']) > 1


def test_existing_case_connection_and_disjoint_deposit_rows_keep_ledger_provenance(review_api):
    client, app, _, _ = review_api
    review = run(client, app, upload(client, create(client, auto_process=True), sample_pdf(('payslip', 'deposit', 'deposit'))))
    review = prepare(client, review)
    wid = checked(client.post('/api/v1/workplaces', json={'name': 'Cafe ABC'}), 201)['workplaceId']
    cid = checked(client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'MONTHLY',
        'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}), 201)['caseId']
    exported = complete(client, review, case_id=cid, analyze=False)
    assert exported['case_id'] == cid
    with app.state.db.connect() as con:
        docs = [dict(row) for row in con.execute('''SELECT d.id,e.id AS candidate_id FROM documents d
            JOIN extraction_candidates e ON e.document_id=d.id WHERE d.document_type='DEPOSIT' ORDER BY d.id''')]
    assert len(docs) == 2
    for doc in docs:
        body = {'paymentMethod': 'BANK_TRANSFER', 'transactionDate': '2026-11-10', 'amount': 2000000,
            'source': 'DOCUMENT', 'confirmationStatus': 'USER_CONFIRMED', 'sourceDocumentId': doc['id'],
            'sourceCandidateId': doc['candidate_id'], 'sourceRowId': 'transaction_1'}
        response = client.post(f'/api/v1/workplaces/{wid}/payment-transactions', json=body)
        saved = checked(response, 201)
        assert saved['provenance']['source_range'] in ([2, 2], [3, 3])
        assert client.post(f'/api/v1/workplaces/{wid}/payment-transactions', json=body).status_code == 409


def test_explicit_standard_conditions_reach_existing_wage_engine(review_api):
    from test_runtime_db import confirmation_from
    from agent.tests.test_standard_wages import standard_snapshot
    client, app, _, _ = review_api
    authored = standard_snapshot('M-STD')
    confirmed = confirmation_from(authored)
    review = run(client, app, upload(client, create(client, auto_process=True), sample_pdf(('contract', 'payslip'))))
    amount = next(f.value for f in authored.confirmed_fields if f.field_id == 'contract_rate')
    for field in list(all_fields(review)):
        value = None
        if field['field_id'] in ('contract_start', 'period_start'):
            value = str(authored.period.start)
        elif field['field_id'] in ('contract_end', 'period_end'):
            value = str(authored.period.end)
        elif field['field_id'] == 'contract_rate':
            value = str(amount)
        if value:
            review = checked(client.put('/api/v1/reviews/' + review['id'] + '/fields', json={
                'revision': review['revision'], 'field_id': field['id'], 'value': value}))
    review = checked(client.put('/api/v1/reviews/' + review['id'], json={'revision': review['revision'],
        'basic': {'employer_name': 'Cafe ABC', 'pay_type': 'MONTHLY', 'period_start': str(authored.period.start),
            'period_end': str(authored.period.end)}, 'options': {'subjects_confirmed': True}, 'confirm_basic': True}))
    review = checked(client.post('/api/v1/reviews/' + review['id'] + '/confirm', json={
        'revision': review['revision'], 'source_seen': True, 'field_ids': [f['id'] for f in all_fields(review) if f['value']]}))
    calculation = {key: confirmed[key] for key in ('wage_conditions', 'work_records', 'coverage', 'context_requirements')}
    calculation['wage_conditions']['statement_mappings'] = []
    exported = complete(client, review, calculation_input=calculation)
    client.portal.call(app.state.service.execute_job, exported['job_id'])
    job = checked(client.get('/api/v1/agent-jobs/' + exported['job_id']))
    assert job['status'] == 'COMPLETED', job
    assert job['output']['wage_section']['gross_total'] == '3272727', job['output']['wage_section']
