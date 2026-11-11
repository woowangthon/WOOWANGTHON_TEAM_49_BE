"""Exercise a running deployment with synthetic data, without paid model calls.

Creates a synthetic account, workplace, case, reading and report. Keep this out
of liveness probes: the records intentionally remain for persistence checks.
"""
import argparse
import io
import json
import secrets
import time
import uuid

import httpx
from pypdf import PdfReader
from reportlab.pdfgen import canvas


def check(base_url, timeout):
    identifier = uuid.uuid4().hex
    with httpx.Client(base_url=base_url.rstrip('/'), timeout=15) as client:
        def request(method, path, expected=200, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.status_code != expected:
                # Do not dump login tokens, cookies or request bodies on failure.
                raise RuntimeError(f'{method} {path}: HTTP {response.status_code}, expected {expected}')
            return response.json()['data']

        def wait(job_id):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                job = request('GET', f'/api/v1/agent-jobs/{job_id}')
                if job['status'] == 'COMPLETED':
                    return job
                if job['status'] in ('FAILED', 'CANCELLED', 'STALE'):
                    raise RuntimeError(f'Job {job_id} ended as {job["status"]}')
                time.sleep(.2)
            raise TimeoutError(f'Job {job_id} did not finish within {timeout}s')

        health = request('GET', '/health')
        assert health['aiMode'] == 'direct'
        credentials = {'email': f'deploy-{identifier}@example.test', 'password': secrets.token_urlsafe(24)}
        request('POST', '/api/v1/auth/signup', 201, json={**credentials, 'name': '합성 배포 점검'})
        token = request('POST', '/api/v1/auth/login', json=credentials)['accessToken']
        client.headers['Authorization'] = f'Bearer {token}'
        wid = request('POST', '/api/v1/workplaces', 201, json={'name': 'SYNTHETIC deployment check'})['workplaceId']
        period = {'start': '2026-10-01', 'end': '2026-10-31'}
        cid = request('POST', '/api/v1/cases', 201, json={'workplaceId': wid, 'payType': 'HOURLY',
            'periodStart': period['start'], 'periodEnd': period['end']})['caseId']

        raw = io.BytesIO()
        pdf = canvas.Canvas(raw)
        pdf.drawString(50, 700, 'SYNTHETIC deployment PDF - amount 2000000 KRW - not a real payslip')
        pdf.save()
        did = request('POST', f'/api/v1/workplaces/{wid}/documents', 202,
            files={'file': ('synthetic.pdf', raw.getvalue(), 'application/pdf')},
            data={'documentType': 'PAYSLIP'})['documentId']
        reading = request('POST', f'/api/v1/documents/{did}/read', 202)
        wait(reading['jobId'])
        history = request('GET', f'/api/v1/documents/{did}/processing-history')
        assert history['readings'], 'PDF reading was not persisted'

        review = request('POST', '/api/v1/reviews', 201,
            json={'title': 'SYNTHETIC deployment review', 'auto_process': False, 'auto_verify': False})
        review_id = review['id']
        request('POST', f'/api/v1/reviews/{review_id}/documents', 202,
            files={'files': ('synthetic-review.pdf', raw.getvalue(), 'application/pdf')},
            data={'revision': str(review['revision'])})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            review = request('GET', f'/api/v1/reviews/{review_id}')
            if review['jobs'] and all(job['status'] == 'COMPLETED' for job in review['jobs']):
                break
            if any(job['status'] in ('FAILED', 'CANCELLED', 'INTERRUPTED') for job in review['jobs']):
                raise RuntimeError('Review reading failed')
            time.sleep(.2)
        else:
            raise TimeoutError('Review reading did not finish')
        review_document_id = review['documents'][0]['id']
        preview = client.get(f'/api/v1/reviews/{review_id}/documents/{review_document_id}/pages/1.png')
        assert preview.status_code == 200 and preview.content.startswith(b'\x89PNG')

        gid = f'default-{cid}'
        group = request('GET', f'/api/v1/comparison-groups/{gid}')
        fields = [{'field_id': key, 'value': amount, 'unit': 'KRW',
            'confirmation_id': 'synthetic-deployment', 'period': period}
            for key, amount in [('gross', '2000000'), ('deduction', '200000'), ('net', '1800000')]]
        request('PUT', f'/api/v1/comparison-groups/{gid}/confirmed-input', json={
            'expectedVersion': group['version'], 'confirmedInput': {
                'profile': 'H-STD', 'subjects_confirmed': True, 'confirmed_fields': fields}})
        run = request('POST', f'/api/v1/comparison-groups/{gid}/analyze', 202,
            headers={'Idempotency-Key': f'smoke-{identifier}'})
        result_id = wait(run['jobId'])['result_id']
        result = request('GET', f'/api/v1/agent-results/{result_id}')['result']
        arithmetic = next(item for item in result['comparisons'] if item['rule_id'] == 'R-05')
        assert arithmetic['status'] == 'match' and arithmetic['delta'] == '0'
        explanation = request('POST', f'/api/v1/agent-results/{result_id}/explanations', 202)
        wait(explanation['jobId'])
        report = request('POST', f'/api/v1/agent-results/{result_id}/reports', 202,
            json={'explanationJobId': explanation['jobId'], 'documentIds': []})
        artifacts = wait(report['jobId'])['output']['artifacts']
        artifact = next(item for item in artifacts if item['mime_type'] == 'application/pdf')
        response = client.get('/api/v1/artifacts/' + artifact['artifact_id'])
        assert response.status_code == 200 and response.content.startswith(b'%PDF')
        text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
        assert '2,000,000' in text and period['start'] in text
        request('POST', f'/api/v1/agent-results/{result_id}/inquiry')
        return {'status': 'passed', 'ai_mode': health['aiMode'], 'paid_model_calls': 0,
            'workplace_id': wid, 'case_id': cid, 'document_id': did, 'result_id': result_id,
            'pdf_artifact_id': artifact['artifact_id'], 'pdf_bytes': len(response.content),
            'review_id': review_id, 'review_preview_bytes': len(preview.content)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--timeout', type=float, default=90)
    args = parser.parse_args()
    print(json.dumps(check(args.base_url, args.timeout), ensure_ascii=False, indent=2))
