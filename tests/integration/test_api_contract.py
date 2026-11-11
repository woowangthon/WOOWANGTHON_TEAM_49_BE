"""Real HTTP contract, SQLite and direct agent worker; no model/OCR needed here."""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app


@pytest.fixture
def api(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path/'data', workers_enabled=False))
    with TestClient(app) as client:
        client.post('/api/v1/auth/signup', json={'email':'api@example.com','password':'secure-test-password','name':'합성 사용자'})
        token=client.post('/api/v1/auth/login',json={'email':'api@example.com','password':'secure-test-password'}).json()['data']['accessToken']
        client.headers['Authorization']='Bearer '+token
        wid=client.post('/api/v1/workplaces',json={'name':'합성 사업장'}).json()['data']['workplaceId']
        cid=client.post('/api/v1/cases',json={'workplaceId':wid,'payType':'HOURLY','periodStart':'2026-10-01','periodEnd':'2026-10-31'}).json()['data']['caseId']
        yield client, app, wid, cid


def save_input(client,gid,version=1):
    body={'profile':'H-STD','subjects_confirmed':True,'confirmed_fields':[
        {'field_id':'gross','value':'2000000','unit':'KRW','confirmation_id':'api-confirm'},
        {'field_id':'deduction','value':'200000','unit':'KRW','confirmation_id':'api-confirm'},
        {'field_id':'net','value':'1800000','unit':'KRW','confirmation_id':'api-confirm'},
    ]}
    for field in body['confirmed_fields']:
        field['period']={'start':'2026-10-01','end':'2026-10-31'}
    response=client.put(f'/api/v1/comparison-groups/{gid}/confirmed-input',json={'expectedVersion':version,'confirmedInput':body})
    assert response.status_code==200,response.text
    return response.json()['data']


def test_http_group_to_real_agent_result_and_explanation(api):
    client,app,_,cid=api
    gid=f'default-{cid}'
    groups=client.get(f'/api/v1/cases/{cid}/comparison-groups')
    assert groups.status_code==200,groups.text
    assert groups.json()['data']['items'][0]['groupId']==gid
    save_input(client,gid)
    run=client.post(f'/api/v1/comparison-groups/{gid}/analyze',headers={'Idempotency-Key':'analysis-1'})
    assert run.status_code==202,run.text
    jid=run.json()['data']['jobId']
    assert client.post(f'/api/v1/comparison-groups/{gid}/analyze',headers={'Idempotency-Key':'analysis-1'}).json()['data']['jobId']==jid
    asyncio.run(app.state.service.execute_job(jid))
    job=client.get(f'/api/v1/agent-jobs/{jid}').json()['data']
    assert job['status']=='COMPLETED',job
    rid=job['result_id']
    detail=client.get(f'/api/v1/agent-results/{rid}')
    assert detail.status_code==200,detail.text
    result=detail.json()['data']['result']
    r5=next(item for item in result['comparisons'] if item['rule_id']=='R-05')
    assert r5['status']=='match'
    assert r5['delta']=='0'
    assert result['wage_section']['gross_total'] is None
    assert result['presentation']['delta_formula']=='lhs - rhs'
    expl=client.post(f'/api/v1/agent-results/{rid}/explanations')
    assert expl.status_code==202,expl.text
    eid=expl.json()['data']['jobId']
    asyncio.run(app.state.service.execute_job(eid))
    explanation=client.get(f'/api/v1/agent-jobs/{eid}').json()['data']
    assert explanation['status']=='COMPLETED',explanation
    assert explanation['output']['result_id']==rid
    report=client.post(f'/api/v1/agent-results/{rid}/reports',json={'explanationJobId':eid,'documentIds':[]})
    assert report.status_code==202,report.text
    report_job=report.json()['data']['jobId']
    asyncio.run(app.state.service.execute_job(report_job))
    published=client.get(f'/api/v1/agent-jobs/{report_job}').json()['data']
    assert published['status']=='COMPLETED',published
    pdf=next(item for item in published['output']['artifacts'] if item['mime_type']=='application/pdf')
    download=client.get('/api/v1/artifacts/'+pdf['artifact_id'])
    assert download.status_code==200,download.text
    text='\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(download.content)).pages)
    assert '2026-10-01' in text and '2,000,000' in text
    assert client.post(f'/api/v1/agent-results/{rid}/inquiry').status_code==200
    edited=client.post(f'/api/v1/agent-results/{rid}/inquiry',json={'questionIds':[],'text':'사용자가 수정한 회사 문의 문장입니다.'})
    assert edited.status_code==200,edited.text
    assert edited.json()['data']['user_edited'] is True
    assert len(client.get(f'/api/v1/agent-results/{rid}/inquiries').json()['data']['items'])==2
    assert client.post(f'/api/v1/agent-results/{rid}/inquiry',json={'questionIds':['unknown-question']}).status_code==422
    # Existing comparisons endpoint remains result history, never group inventory.
    history=client.get(f'/api/v1/cases/{cid}/comparisons')
    assert history.status_code==200,history.text
    assert history.json()['data']['items'][0]['result_id']==rid
    assert client.get(f'/api/v1/cases/{cid}').status_code==200
    assert client.delete('/api/v1/artifacts/'+pdf['artifact_id']).status_code==204
    assert client.get('/api/v1/artifacts/'+pdf['artifact_id']).status_code==404


def test_http_version_ownership_contract_and_deletion(api):
    client,app,_,cid=api
    gid=f'default-{cid}'
    save_input(client,gid)
    stale=client.put(f'/api/v1/comparison-groups/{gid}/confirmed-input',json={'expectedVersion':1,'confirmedInput':{}})
    assert stale.status_code==409
    version=client.get(f'/api/v1/comparison-groups/{gid}').json()['data']['version']
    changed=client.patch(f'/api/v1/cases/{cid}',json={'scheduledPayDate':'2026-11-10'})
    assert changed.status_code==200,changed.text
    refreshed=client.get(f'/api/v1/comparison-groups/{gid}').json()['data']
    assert refreshed['version']==version+1 and refreshed['needsReview'] is True
    stale=client.put(f'/api/v1/comparison-groups/{gid}/confirmed-input',json={'expectedVersion':version,'confirmedInput':{}})
    assert stale.status_code==409
    save_input(client,gid,refreshed['version'])
    assert client.get('/api/v1/agent-contracts').status_code==200
    assert client.get('/api/v1/glossary?q=주휴').status_code==200
    jid=client.post(f'/api/v1/comparison-groups/{gid}/analyze').json()['data']['jobId']
    asyncio.run(app.state.service.execute_job(jid))
    rid=client.get(f'/api/v1/agent-jobs/{jid}').json()['data']['result_id']
    client.post('/api/v1/auth/signup',json={'email':'other@example.com','password':'secure-test-password','name':'다른 사용자'})
    token=client.post('/api/v1/auth/login',json={'email':'other@example.com','password':'secure-test-password'}).json()['data']['accessToken']
    assert client.get(f'/api/v1/agent-results/{rid}',headers={'Authorization':'Bearer '+token}).status_code==404
    assert client.delete(f'/api/v1/cases/{cid}').status_code==204
    assert client.get(f'/api/v1/agent-results/{rid}').status_code==404


def test_free_chat_missing_model_is_explicit_error(api):
    client,app,wid,cid=api
    app.state.settings.openai_api_key=None
    chat=client.post(f'/api/v1/workplaces/{wid}/chats',json={'defaultScope':{'scopeType':'CASE','caseId':cid}})
    assert chat.status_code==201,chat.text
    chat_id=chat.json()['data']['chatId']
    response=client.post(f'/api/v1/workplaces/{wid}/chats/{chat_id}/messages',json={'content':'급여 자료를 설명해 주세요.'})
    assert response.status_code==200
    assert 'MODEL_NOT_CONFIGURED' in response.text
    assert 'Mock' not in response.text
