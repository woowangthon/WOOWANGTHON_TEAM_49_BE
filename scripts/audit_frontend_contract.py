"""Audit supplied legacy frontend contracts against the current FastAPI app.

All databases/files/accounts are synthetic and temporary. The external model is
explicitly replaced only in this audit; run_task, rules, mapping and storage are
real. --strict exits nonzero when contract differences are found.
"""
import argparse
import asyncio
import json
import re
import tempfile
import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

import app.agent_service as service_module
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import pdf_bytes
from app.config import Settings
from app.db import load
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


class SyntheticChat:
    def __init__(self):
        self.calls = []

    async def chat(self, payload):
        self.calls.append({'scope': payload['scope'], 'history_count': len(payload['history']),
            'case_ids': [item['case_id'] for item in payload['context']]})
        return {'content': '합성 계약 검증 답변입니다.', 'sources': [], 'thread_id': 'audit-synthetic'}


def audit():
    reference = json.loads((ROOT / 'docs/contract-audit/reference-endpoints.json').read_text())
    traces, findings, probes = [], [], []
    chat = SyntheticChat()
    original = service_module.run_task

    async def traced(task, **kwargs):
        result = await original(task, **kwargs)
        traces.append({'task_kind': task.context.task_kind, 'payload_type': type(task.payload).__name__,
            'outcome': result.status, 'real_agent_runner': True})
        return result

    def compare(name, expected, observed, baseline='legacy-integration', severity='P1'):
        findings.append({'name': name, 'baseline': baseline, 'severity': severity,
            'matches': expected == observed, 'expected': expected, 'observed': observed})

    service_module.run_task = traced
    try:
        with tempfile.TemporaryDirectory(prefix='frontend-contract-') as temporary:
            app = create_app(Settings(_env_file=None, data_dir=Path(temporary), workers_enabled=False,
                password_reset_delivery='file'), provider=FixtureProvider(), ai_client=chat)
            with TestClient(app, raise_server_exceptions=False) as client:
                schema = client.get('/openapi.json').json()
                def normalize(path):
                    return re.sub(r'\{[^}]+\}', '{}', path)
                operations = {(method.upper(), normalize(path)) for path, item in schema['paths'].items()
                    for method in item if method in {'get', 'post', 'put', 'patch', 'delete'}}
                exported = json.loads((ROOT / 'docs/openapi.json').read_text())
                export_operations = {(method.upper(), normalize(path)) for path, item in exported['paths'].items()
                    for method in item if method in {'get', 'post', 'put', 'patch', 'delete'}}
                untyped = sum(1 for item in schema['paths'].values()
                    for method, definition in item.items() if method in {'get', 'post', 'put', 'patch', 'delete'}
                    for status, response in definition['responses'].items() if status.startswith('2')
                    and 'application/json' in response.get('content', {})
                    and not response['content']['application/json'].get('schema'))
                inventory = []
                for entry in reference['endpoints']:
                    path = '/api/v1' + entry['path']
                    concrete = re.sub(r'\{[^}]+\}', '1', path)
                    response = client.request(entry['method'], concrete,
                        **({'json': {}} if entry['method'] in ('POST', 'PUT', 'PATCH') else {}))
                    inventory.append({**entry,
                        'registered': (entry['method'], normalize(path)) in operations,
                        'unauthenticated_http_status': response.status_code,
                        'scope': 'route/authentication probe; not a successful DTO test'})

                def req(method, path, expected=200, **kwargs):
                    response = client.request(method, '/api/v1' + path, **kwargs)
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    probes.append({'method': method, 'path': path, 'http_status': response.status_code,
                        'expected_current_status': expected,
                        'error_code': (body.get('data') or {}).get('errorCode') if isinstance(body, dict)
                            and isinstance(body.get('data'), dict) else None})
                    if expected is not None and response.status_code != expected:
                        raise AssertionError(f'{method} {path}: HTTP {response.status_code}, expected {expected}')
                    if body is not None:
                        assert body['code'] == response.status_code
                    return response, body['data'] if body else None

                creds = {'email': 'contract-audit@example.test', 'password': 'synthetic-audit-password'}
                req('POST', '/auth/signup', 201, json={**creds, 'name': '합성 계약 점검'})
                _, login = req('POST', '/auth/login', json=creds)
                client.headers['Authorization'] = 'Bearer ' + login['accessToken']
                _, w = req('POST', '/workplaces', 201, json={'name': 'SYNTHETIC audit workplace'})
                wid = w['workplaceId']
                compare('workplace_create_full_DTO', True, 'name' in w, 'proposal-v0.5', 'P2')
                _, c = req('POST', '/cases', 201, json={'workplaceId': wid, 'payType': 'HOURLY',
                    'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'})
                cid, gid = c['caseId'], f'default-{c["caseId"]}'
                compare('case_create_automatic_job', True, c['recompute']['scheduled'], severity='P0')
                _, manual = req('POST', f'/cases/{cid}/manual-records', 201, json={
                    'recordType': 'PAYSLIP', 'source': 'USER_INPUT',
                    'data': {'grossPay': 2300000, 'totalDeductions': 200000, 'netPay': 2090000}})
                compare('manual_save_automatic_job', True, manual['recompute']['scheduled'], severity='P0')
                _, unavailable = req('POST', f'/cases/{cid}/payslip-validations', None, json={'confirmed': True})
                compare('legacy_manual_values_are_calculation_input', 202, probes[-1]['http_status'], severity='P0')
                _, conditions = req('PUT', f'/cases/{cid}/conditions', json={'answers': [
                    {'conditionId': 'regular_minutes', 'value': 10000},
                    {'conditionId': 'base_scope_confirmed', 'value': True}]})
                compare('conditions_save_automatic_job', True, conditions['recompute']['scheduled'], severity='P0')
                _, ready = req('GET', f'/cases/{cid}/readiness')
                compare('readiness_mode_enum', True,
                    all(item['mode'] in {'FULL', 'PARTIAL', 'NONE'} for item in ready['checks']))
                req('POST', f'/cases/{cid}/recompute', None,
                    json={'checks': ['PAYSLIP_VALIDATION'], 'reason': 'USER_RETRY', 'expectedInputVersion': 4})
                compare('proposal_recompute_request_accepted', 202, probes[-1]['http_status'], 'proposal-v0.5')

                raw = pdf_bytes(['SYNTHETIC AUDIT PAYSLIP', 'gross: 2300000 KRW',
                    'deduction: 200000 KRW', 'net: 2090000 KRW'])
                _, document = req('POST', f'/cases/{cid}/documents', 202,
                    files={'file': ('synthetic.pdf', raw, 'application/pdf')}, data={'documentType': 'PAYSLIP'})
                did = document['documentId']
                asyncio.run(app.state.service.read_document(1, did))
                _, read_fields = req('GET', f'/cases/{cid}/documents/{did}/fields')
                compare('legacy_document_processing_status_enum', True, read_fields['processingStatus'] in
                    {'UPLOADED', 'PROCESSING', 'READY', 'REVIEW_REQUIRED', 'FAILED'})
                extraction_id = app.state.service.enqueue_extraction(1, did)
                asyncio.run(app.state.service.execute_job(extraction_id))
                assert app.state.service.store.job(1, extraction_id)['status'] == 'COMPLETED'
                _, fields = req('GET', f'/cases/{cid}/documents/{did}/fields')
                gross = next(item for item in fields['fields'] if item['key'] == 'gross')
                compare('legacy_extracted_numeric_keys', True, any(item['key'] == 'grossPay' for item in fields['fields']))
                compare('legacy_extracted_money_type', 'MONEY_KRW', gross['valueType'])
                req('PATCH', f'/cases/{cid}/documents/{did}/fields/{gross["fieldId"]}', None,
                    json={'value': 2350000, 'confirmed': True})
                compare('legacy_numeric_field_patch', 200, probes[-1]['http_status'], severity='P0')
                req('PATCH', f'/cases/{cid}/documents/{did}/fields/{gross["fieldId"]}', None,
                    json={'value': '2350000', 'confirmed': True})
                compare('legacy_string_field_patch', 200, probes[-1]['http_status'], severity='P0')
                candidate = app.state.service.store.job(1, extraction_id)['output']['candidate_id']
                _, choice = req('POST', f'/documents/{did}/candidate-reviews', json={
                    'candidateId': candidate, 'fieldId': 'gross', 'manual': True, 'value': '2350000'})
                compare('current_candidate_confirmation', 'CONFIRMED', choice['status'], 'agent-orchestration', 'P0')

                long_pdf = io.BytesIO()
                writer = Canvas(long_pdf)
                for number in range(21):
                    writer.drawString(50, 700, f'SYNTHETIC page {number+1} salary 2000000 KRW')
                    writer.showPage()
                writer.save()
                _, long_document = req('POST', f'/workplaces/{wid}/documents', 202,
                    files={'file': ('synthetic-21-pages.pdf', long_pdf.getvalue(), 'application/pdf')},
                    data={'documentType': 'PAYSLIP'})
                long_read = asyncio.run(app.state.service.read_document(1, long_document['documentId']))
                compare('accepted_21_page_PDF_can_be_read', 'COMPLETED', long_read['status'], severity='P1')
                compare('accepted_21_page_PDF_read_error', None, long_read['error_code'], severity='P1')
                readable = asyncio.run(app.state.service.recognizer(long_pdf.getvalue(),
                    app.state.agent_settings.model_copy(update={'max_pages': 50})))
                assert readable['page_count'] == 21 and readable['source_method'] == 'pdf_text'
                findings[-1]['diagnostic_only'] = 'Same bytes read as 21 text pages with a local max_pages=50 settings copy; production configuration unchanged.'

                _, group = req('GET', f'/comparison-groups/{gid}')
                period = {'start': '2026-10-01', 'end': '2026-10-31'}
                confirmed = {'profile': 'H-STD', 'subjects_confirmed': True, 'confirmed_fields': [
                    {'field_id': key, 'value': value, 'unit': 'KRW', 'confirmation_id': 'synthetic-audit', 'period': period}
                    for key, value in [('gross', '2300000'), ('deduction', '200000'), ('net', '2090000')]]}
                req('PUT', f'/comparison-groups/{gid}/confirmed-input',
                    json={'expectedVersion': group['version'], 'confirmedInput': confirmed})
                _, queued = req('POST', f'/cases/{cid}/payslip-validations', 202, json={'confirmed': True})
                asyncio.run(app.state.service.execute_job(queued['jobId']))
                current_job = app.state.service.store.job(1, queued['jobId'])
                assert current_job['status'] == 'COMPLETED'
                result_id = current_job['result_id']
                _, detail = req('GET', f'/agent-results/{result_id}')
                r5 = next(row for row in detail['result']['comparisons'] if row['rule_id'] == 'R-05')
                compare('real_agent_R05_delta', '10000', r5['delta'], 'agent-orchestration', 'P0')
                compare('real_agent_R05_difference', 'difference', r5['status'], 'agent-orchestration', 'P0')
                _, result = req('GET', f'/cases/{cid}/results')
                compare('legacy_result_summary_counts', True, 'counts' in result['summary'], severity='P0')
                compare('legacy_result_item_DTO', True, all(
                    {'id', 'title', 'calculatedValue', 'documentValue', 'sources'} <= item.keys()
                    for item in result['items']), severity='P0')
                compare('legacy_payslip_validation_section', True, result['payslipValidation'] is not None, severity='P0')
                compare('legacy_numeric_snapshot_ID', True,
                    type(result['versions']['calculation']['snapshotId']) is int)
                _, snapshots = req('GET', f'/cases/{cid}/payslip-validations')
                compare('legacy_validation_snapshot_DTO', True,
                    'validationId' in snapshots['items'][0] and 'paymentTotal' in snapshots['items'][0])
                refs = [item['snapshotId'] for item in result['versions'].values()]
                compare('independent_result_snapshots', True, len(set(refs)) > 1)
                _, status = req('GET', f'/cases/{cid}/recompute-status')
                compare('legacy_recompute_results_refs', True, 'results' in status)
                compare('legacy_job_camelcase', True, 'jobId' in status['latestJob'])

                _, tx = req('POST', f'/workplaces/{wid}/payment-transactions', 201, json={
                    'paymentMethod': 'BANK_TRANSFER', 'transactionDate': '2026-11-01', 'amount': 2090000,
                    'source': 'USER_INPUT', 'confirmationStatus': 'USER_CONFIRMED', 'bankReference': 'SYNTHETIC-1'})
                req('POST', f'/cases/{cid}/deposits', 201,
                    json={'transactionId': tx['transactionId'], 'allocatedAmount': 2090000})
                _, changed = req('GET', f'/cases/{cid}/results')
                compare('allocation_changes_only_deposit_freshness', 'CURRENT', changed['versions']['calculation']['freshness'])
                req('POST', f'/cases/{cid}/reports', None, json={'includeOriginalDocuments': False,
                    'documentIds': [], 'useLatestAvailableResults': True, 'acknowledgeOutdatedResults': False})
                compare('legacy_report_create_endpoint', 202, probes[-1]['http_status'], severity='P0')
                req('POST', f'/cases/{cid}/company-inquiry', None, json={'resultItemIds': ['unknown-result'],
                    'tone': 'POLITE', 'basedOnResultVersions': {'calculation': 999999}})
                compare('inquiry_rejects_unknown_or_stale_selection', True, probes[-1]['http_status'] in (409, 422))

                _, session = req('POST', f'/workplaces/{wid}/chats', 201,
                    json={'defaultScope': {'scopeType': 'CASE', 'caseId': cid}})
                chat_id = session['chatId']
                cp = f'/workplaces/{wid}/chats/{chat_id}'
                req('POST', cp + '/messages', None,
                    json={'content': '합성 질문', 'clientMessageId': 'synthetic-stable-id'})
                compare('proposal_clientMessageId_accepted', 200, probes[-1]['http_status'], 'proposal-v0.5', 'P2')
                stream = client.post('/api/v1' + cp + '/messages', json={'content': '합성 질문'})
                events = re.findall(r'^event: (.+)$', stream.text, re.M)
                compare('current_SSE_events', ['message_start', 'content_delta', 'sources', 'message_done'],
                    events, 'agent-orchestration', 'P0')
                assert stream.status_code == 200 and 'text/event-stream' in stream.headers['content-type']
                _, chat_detail = req('GET', cp)
                compare('proposal_chat_detail_paginated', True,
                    'session' in chat_detail and isinstance(chat_detail.get('messages'), dict), 'proposal-v0.5')
                compare('current_chat_persisted_roles', ['USER', 'ASSISTANT'],
                    [m['role'] for m in chat_detail['messages']], 'agent-orchestration', 'P0')
                client.post('/api/v1' + cp + '/messages', json={'content': '이어서 질문'})
                compare('current_chat_history_forwarded', 2, chat.calls[-1]['history_count'], 'agent-orchestration', 'P0')
                client.post('/api/v1' + cp + '/messages', json={'content': '전체 질문', 'scope': {'scopeType': 'WORKPLACE_ALL'}})
                compare('current_chat_scope_history_isolation', 0, chat.calls[-1]['history_count'], 'agent-orchestration', 'P0')

                _, explanation = req('POST', f'/agent-results/{result_id}/explanations', 202)
                asyncio.run(app.state.service.execute_job(explanation['jobId']))
                compare('current_explanation_job', 'COMPLETED',
                    app.state.service.store.job(1, explanation['jobId'])['status'], 'agent-orchestration', 'P0')
                _, report = req('POST', f'/agent-results/{result_id}/reports', 202,
                    json={'explanationJobId': explanation['jobId'], 'documentIds': []})
                asyncio.run(app.state.service.execute_job(report['jobId']))
                report_job = app.state.service.store.job(1, report['jobId'])
                compare('current_report_job', 'COMPLETED', report_job['status'], 'agent-orchestration', 'P0')
                artifacts = report_job['output']['artifacts']
                contents = {a['mime_type']: client.get('/api/v1/artifacts/' + a['artifact_id']).content
                    for a in artifacts}
                with zipfile.ZipFile(io.BytesIO(contents['application/zip'])) as archive:
                    compare('current_ZIP_contains_same_PDF', True,
                        contents['application/pdf'] == archive.read('summary.pdf'), 'agent-orchestration', 'P0')

                with app.state.db.connect() as con:
                    checks = load(con.execute('SELECT payload_json FROM agent_jobs WHERE id=?', (queued['jobId'],)).fetchone()[0])
                    compare('selected_check_forwarded_to_agent', True, 'checks' in checks)
                    assert not con.execute('PRAGMA foreign_key_check').fetchall()

                return {'scope': {'full_suite_separate': True, 'contract_operations': 92,
                            'route_probes_are_not_full_DTO_coverage': True, 'paid_model_calls': 0,
                            'external_provider': 'explicit FixtureProvider + SyntheticChat only',
                            'transport': 'FastAPI TestClient; real TCP checked separately'},
                    'openapi': {'paths': len(schema['paths']), 'operations': len(operations),
                        'export_operations_match': operations == export_operations,
                        'json_success_without_schema': untyped},
                    'reference_sources': {key: value for key, value in reference.items() if key != 'endpoints'},
                    'inventory': inventory, 'findings': findings, 'probes': probes,
                    'agent_call_trace': traces, 'chat_call_trace': chat.calls,
                    'summary': {'registered': sum(item['registered'] for item in inventory),
                        'missing': sum(not item['registered'] for item in inventory),
                        'scenario_matches': sum(item['matches'] for item in findings),
                        'scenario_differences': sum(not item['matches'] for item in findings)}}
    finally:
        service_module.run_task = original


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/contract-audit/findings.json')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    data = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(data['summary'], ensure_ascii=False))
    if args.strict and (data['summary']['missing'] or data['summary']['scenario_differences']):
        raise SystemExit(1)
