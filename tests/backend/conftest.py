import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / 'data', recompute_debounce_seconds=.02)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as session:
        yield session


def register(client, email='worker@example.com'):
    response = client.post('/api/v1/auth/signup', json={'email': email, 'password': 'password1234', 'name': '테스트 근로자'})
    assert response.status_code == 201, response.text
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': 'password1234'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['data']['accessToken']}


@pytest.fixture
def auth(client):
    return register(client)


def create_workplace(client, auth):
    r = client.post('/api/v1/workplaces', headers=auth, json={'name': '카페 테스트', 'region': '청주시'})
    assert r.status_code == 201, r.text
    return r.json()['data']['workplaceId']


def create_case(client, auth, workplace):
    r = client.post('/api/v1/cases', headers=auth, json={'workplaceId': workplace, 'payType': 'HOURLY',
        'periodStart': '2026-10-03', 'periodEnd': '2026-11-02'})
    assert r.status_code == 201, r.text
    return r.json()['data']['caseId']


@pytest.fixture
def workspace(client, auth):
    wid = create_workplace(client, auth)
    return wid, create_case(client, auth, wid)


def wait_for(client, path, auth, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get('/api/v1' + path, headers=auth)
        assert r.status_code == 200, r.text
        last = r.json()['data']
        if predicate(last):
            return last
        time.sleep(.03)
    raise AssertionError(f'poll timeout: {last}')


def wait_results(client, cid, auth):
    return wait_for(client, f'/cases/{cid}/results', auth, lambda d: d['freshness']['overall'] == 'CURRENT')


def image_file():
    stream = io.BytesIO()
    Image.new('RGB', (100, 80), 'white').save(stream, format='PNG')
    return stream.getvalue()


def upload(client, auth, cid, kind='PAYSLIP'):
    r = client.post(f'/api/v1/cases/{cid}/documents', headers=auth,
                    files={'file': ('급여.png', image_file(), 'image/png')}, data={'documentType': kind})
    assert r.status_code == 202, r.text
    did = r.json()['data']['documentId']
    doc = wait_for(client, f'/cases/{cid}/documents/{did}', auth,
                   lambda d: d['processingStatus'] not in ('UPLOADED', 'PROCESSING'))
    return did, doc


def manual_payslip(client, auth, cid, **overrides):
    payload = {'grossPay': 2300000, 'totalDeductions': 200000, 'netPay': 2090000,
               'paymentItems': [2000000, 300000], 'deductionItems': [190000]}
    payload.update(overrides)
    r = client.post(f'/api/v1/cases/{cid}/manual-records', headers=auth,
                    json={'recordType': 'PAYSLIP', 'data': payload})
    assert r.status_code == 201, r.text
    return r.json()['data']['recordId']
