import asyncio

import httpx
import pytest

from app.ai import AIAdapter, AnalysisResponse
from app.errors import ApiError
from app.main import create_app
from fastapi.testclient import TestClient
from tests.backend.conftest import create_case, create_workplace, register, upload, wait_results


def upstream_response(document_id=1):
    return {'document_id': document_id, 'document_type': 'payslip', 'fields': [
        {'key': 'netPay', 'label': '실수령액', 'value': 100, 'value_type': 'MONEY_KRW',
         'status': 'UNCONFIRMED', 'source': {'page': 1, 'text': '100원', 'bounding_box': [0, 0, .2, .1]}}],
        'quality_issues': [], 'thread_id': 'upstream-thread'}


def test_http_adapter_multipart_and_chat_context(settings, tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/analyze':
            assert request.headers['content-type'].startswith('multipart/form-data')
            assert b'name="document_id"' in request.content
            assert b'fake-pdf-file-content' in request.content
            return httpx.Response(200, json=upstream_response(9))
        return httpx.Response(200, json={'content': '정상 응답', 'sources': [], 'thread_id': 'chat-thread'})
    async def run():
        settings.ai_mode = 'http'
        adapter = AIAdapter(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        path = tmp_path / 'sample.pdf'
        path.write_bytes(b'fake-pdf-file-content')
        result = await adapter.analyze({'id': 9, 'document_type': 'PAYSLIP', 'file_name': 'sample.pdf', 'media_type': 'application/pdf'}, path)
        assert result.fields[0].value == 100
        answer = await adapter.chat({'thread_id': 'chat-1-case-1', 'scope': {'scopeType': 'CASE', 'caseId': 1}})
        assert answer.content == '정상 응답'
        await adapter.close()
    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.parametrize('failure,expected', [('timeout', 'UPSTREAM_TIMEOUT'), ('http', 'UPSTREAM_UNAVAILABLE'),
                                            ('json', 'UPSTREAM_INVALID_RESPONSE'), ('schema', 'UPSTREAM_INVALID_RESPONSE')])
def test_upstream_errors(settings, failure, expected):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('timed out', request=request)
        if failure == 'http':
            return httpx.Response(503)
        if failure == 'json':
            return httpx.Response(200, text='invalid json')
        return httpx.Response(200, json={'content': 123})
    async def run():
        settings.ai_mode = 'http'
        adapter = AIAdapter(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(ApiError) as error:
            await adapter.chat({'thread_id': 'chat'})
        assert error.value.code == expected
        await adapter.close()
    asyncio.run(run())


def test_invalid_known_field_types_rejected():
    body = upstream_response()
    body['fields'][0]['value_type'] = 'TEXT'
    body['fields'][0]['value'] = '100'
    with pytest.raises(ValueError):
        AnalysisResponse.model_validate(body)
    body = upstream_response()
    body['fields'][0]['source']['bounding_box'] = [.9, .9, .5, .5]
    with pytest.raises(ValueError):
        AnalysisResponse.model_validate(body)


def test_failed_document_does_not_block_other_checks_or_retry(settings):
    call_count = 0
    def handler(request):
        nonlocal call_count
        if request.url.path == '/analyze':
            call_count += 1
            if call_count == 1:
                return httpx.Response(503)
            return httpx.Response(200, json=upstream_response())
        return httpx.Response(200, json={'content': '답변', 'sources': []})
    settings.ai_mode = 'http'
    ai_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_app(settings, ai_client)) as client:
        auth = register(client)
        wid = create_workplace(client, auth)
        cid = create_case(client, auth, wid)
        did, doc = upload(client, auth, cid)
        assert doc['processingStatus'] == 'FAILED'
        from tests.backend.conftest import manual_payslip, wait_for
        manual_payslip(client, auth, cid)
        assert wait_results(client, cid, auth)['payslipValidation']['paymentTotal']['status'] == 'MATCH'
        assert client.post(f'/api/v1/cases/{cid}/documents/{did}/retry', headers=auth).status_code == 202
        doc = wait_for(client, f'/cases/{cid}/documents/{did}', auth, lambda d: d['processingStatus'] in ('READY', 'FAILED'))
        assert doc['processingStatus'] == 'READY'


def test_chat_scope_filtering_and_upstream_source_validation(settings):
    payloads = []
    def handler(request):
        import json
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload['content'] == '잘못된 출처':
            return httpx.Response(200, json={'content': '다른 자료', 'sources': [{'document_id': 999, 'case_id': 999}]})
        return httpx.Response(200, json={'content': '선택한 자료 기준 응답', 'sources': [], 'thread_id': payload['thread_id']})
    settings.ai_mode = 'http'
    with TestClient(create_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))) as client:
        auth = register(client)
        wid = create_workplace(client, auth)
        c1, c2 = create_case(client, auth, wid), create_case(client, auth, wid)
        cid = client.post(f'/api/v1/workplaces/{wid}/chats', headers=auth,
            json={'defaultScope': {'scopeType': 'CASE', 'caseId': c1}}).json()['data']['chatId']
        endpoint = f'/api/v1/workplaces/{wid}/chats/{cid}/messages'
        assert 'message_done' in client.post(endpoint, headers=auth, json={'content': '첫 질문'}).text
        assert 'message_done' in client.post(endpoint, headers=auth,
            json={'content': '전체 질문', 'scope': {'scopeType': 'WORKPLACE_ALL'}}).text
        assert len(payloads[-1]['context']) == 2 and payloads[-1]['history'] == []
        assert 'message_done' in client.post(endpoint, headers=auth, json={'content': '다시 기간 질문'}).text
        assert len(payloads[-1]['context']) == 1
        assert all('전체 질문' not in h['content'] for h in payloads[-1]['history'])
        response = client.post(endpoint, headers=auth, json={'content': '잘못된 출처'})
        assert 'UPSTREAM_SOURCE_OUT_OF_SCOPE' in response.text and 'message_done' not in response.text
        messages = client.get(endpoint.removesuffix('/messages'), headers=auth).json()['data']['messages']
        assert messages[-1]['status'] == 'FAILED'
        assert client.delete(f'/api/v1/cases/{c1}', headers=auth).status_code == 204
        assert client.get(endpoint.removesuffix('/messages'), headers=auth).json()['data']['messages'] == []
