"""Replacement API preserves edits and shared original while requiring review."""
import asyncio

from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import pdf_bytes
from test_api_contract import api as api_fixture, save_input

api = api_fixture


def test_replacement_preserves_group_confirmation_and_case_override(api):
    client,app,wid,cid=api
    app.state.service.provider=FixtureProvider()
    original=pdf_bytes(['SYNTHETIC PAYSLIP','gross: 2000000 KRW','deduction: 200000 KRW','net: 1800000 KRW'])
    response=client.post(f'/api/v1/cases/{cid}/documents',files={'file':('old.pdf',original,'application/pdf')},data={'documentType':'PAYSLIP'})
    assert response.status_code==202,response.text
    did=response.json()['data']['documentId']
    asyncio.run(app.state.service.read_document(1,did))
    jid=app.state.service.enqueue_extraction(1,did,document_type='payslip')
    asyncio.run(app.state.service.execute_job(jid))
    assert app.state.service.store.job(1,jid)['status']=='COMPLETED'
    gid=f'default-{cid}'
    current=client.get(f'/api/v1/comparison-groups/{gid}').json()['data']
    selected=client.put(f'/api/v1/comparison-groups/{gid}/documents',json={'expectedVersion':current['version'],'documentIds':[did]})
    assert selected.status_code==200,selected.text
    saved=save_input(client,gid,version=selected.json()['data']['version'])
    before=saved['confirmedInput']
    fields=client.get(f'/api/v1/cases/{cid}/documents/{did}/fields').json()['data']['fields']
    gross=next(f for f in fields if f['key']=='gross')
    # Existing legacy case edits may remain after migration; the obsolete edit
    # endpoint is intentionally blocked in the new explicit confirmation flow.
    from app.db import dump, now
    with app.state.db.connect(write=True) as con:
        con.execute('INSERT INTO field_overrides(case_id,field_id,value_json,status,confirmed_at) VALUES(?,?,?,?,?)',
                    (cid,gross['fieldId'],dump('2100000'),'CONFIRMED',now()))
    other=client.post('/api/v1/cases',json={'workplaceId':wid,'payType':'HOURLY','periodStart':'2026-11-01','periodEnd':'2026-11-30'}).json()['data']['caseId']
    assert client.post(f'/api/v1/cases/{other}/document-links',json={'documentId':did}).status_code==201
    replacement=client.post(f'/api/v1/cases/{cid}/documents/{did}/replace',files={'file':('new.pdf',pdf_bytes(['SYNTHETIC REPLACEMENT','gross: 2200000 KRW']),'application/pdf')})
    assert replacement.status_code==202,replacement.text
    target=replacement.json()['data']
    new_id=target['documentId']
    asyncio.run(app.state.service.read_document(1,new_id))
    done=client.post(f'/api/v1/cases/{cid}/document-replacements/{target["replacementId"]}/confirm',json={'confirmed':True})
    assert done.status_code==200,done.text
    after=client.get(f'/api/v1/comparison-groups/{gid}').json()['data']
    assert after['confirmedInput']==before and after['needsReview'] is True
    assert after['documentIds']==[new_id]
    assert client.get(f'/api/v1/cases/{other}/documents/{did}').status_code==200
    assert client.get(f'/api/v1/cases/{cid}/documents/{did}').status_code==404
    fields=client.get(f'/api/v1/cases/{cid}/documents/{new_id}/fields').json()['data']['fields']
    carried=next(f for f in fields if f['key']=='gross')
    assert carried['value']=='2100000' and carried['status']=='UNCONFIRMED'
