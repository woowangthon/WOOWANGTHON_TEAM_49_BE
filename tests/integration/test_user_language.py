"""User language preference and localization of server-written sentences."""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai import chat_instructions
from app.config import Settings
from app.db import Database, dump, now
from app.i18n import CATALOG, Localizer, localize_results, preserves_facts
from app.main import create_app
from app.routers import analysis


class FakeTranslator:
    """Prefixes the language; `broken` drops numbers to exercise the guard."""

    def __init__(self):
        self.calls = []
        self.broken = set()

    def translate(self, texts, language):
        self.calls.append((list(texts), language))
        return [('[번역 누락]' if text in self.broken else f'[{language}] ' + text) for text in texts]


@pytest.fixture
def api(tmp_path):
    translator = FakeTranslator()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / 'data', workers_enabled=False),
                     translation_client=translator)
    with TestClient(app) as client:
        assert client.post('/api/v1/auth/signup', json={
            'email': 'lang@example.test', 'password': 'secure-test-password', 'name': '언어 사용자'}).status_code == 201
        login = client.post('/api/v1/auth/login', json={'email': 'lang@example.test', 'password': 'secure-test-password'})
        client.headers['Authorization'] = 'Bearer ' + login.json()['data']['accessToken']
        yield SimpleNamespace(client=client, app=app, translator=translator, login=login.json()['data'])


def test_language_defaults_to_korean_and_persists_after_change(api):
    assert api.login['user']['language'] == 'ko'
    assert api.client.get('/api/v1/users/me').json()['data']['language'] == 'ko'
    changed = api.client.patch('/api/v1/users/me/language', json={'language': 'en'})
    assert changed.status_code == 200
    assert changed.json() == {'code': 200, 'message': '언어 설정이 변경되었습니다.', 'data': {'language': 'en'}}
    me = api.client.get('/api/v1/users/me').json()['data']
    assert me == {'userId': me['userId'], 'email': 'lang@example.test', 'name': '언어 사용자', 'language': 'en'}
    again = api.client.post('/api/v1/auth/login', json={'email': 'lang@example.test', 'password': 'secure-test-password'})
    assert again.json()['data']['user']['language'] == 'en'
    refreshed = api.client.post('/api/v1/auth/refresh')
    assert refreshed.status_code == 200 and refreshed.json()['data']['user']['language'] == 'en'


@pytest.mark.parametrize('body', [{'language': 'fr'}, {'language': 'EN'}, {}, {'language': 'en', 'extra': 1}])
def test_unsupported_language_is_a_validation_error(api, body):
    response = api.client.patch('/api/v1/users/me/language', json=body)
    assert response.status_code == 422
    assert response.json()['data']['errorCode'] == 'VALIDATION_ERROR'
    assert api.client.get('/api/v1/users/me').json()['data']['language'] == 'ko'


def test_language_change_requires_authentication_and_is_per_user(api):
    anonymous = api.client.patch('/api/v1/users/me/language', json={'language': 'vi'},
                                 headers={'Authorization': ''})
    assert anonymous.status_code == 401
    assert api.client.post('/api/v1/auth/signup', json={
        'email': 'other@example.test', 'password': 'secure-test-password', 'name': '다른 사용자'}).status_code == 201
    other = api.client.post('/api/v1/auth/login', json={'email': 'other@example.test', 'password': 'secure-test-password'})
    other_auth = {'Authorization': 'Bearer ' + other.json()['data']['accessToken']}
    assert api.client.patch('/api/v1/users/me/language', json={'language': 'km'}, headers=other_auth).status_code == 200
    assert api.client.get('/api/v1/users/me').json()['data']['language'] == 'ko'
    assert api.client.get('/api/v1/users/me', headers=other_auth).json()['data']['language'] == 'km'


def test_existing_accounts_are_migrated_to_korean(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, '
                    'password_hash TEXT NOT NULL, deleted_at TEXT)')
        con.execute("INSERT INTO users(email,name,password_hash) VALUES('old@example.test','기존','x:y')")
    Database(path).initialize()
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT language FROM users').fetchall() == [('ko',)]


def test_conditions_and_result_titles_follow_the_saved_language(api):
    wid = api.client.post('/api/v1/workplaces', json={'name': '테스트 사업장'}).json()['data']['workplaceId']
    cid = api.client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'HOURLY',
        'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}).json()['data']['caseId']
    korean = api.client.get(f'/api/v1/cases/{cid}/conditions').json()['data']['conditions']
    assert korean[0]['question'] == CATALOG['condition.workplace_size']['ko']
    api.client.patch('/api/v1/users/me/language', json={'language': 'vi'})
    localized = api.client.get(f'/api/v1/cases/{cid}/conditions').json()['data']['conditions']
    assert [c['question'] for c in localized] == [CATALOG['condition.' + c['conditionId']]['vi'] for c in korean]
    # Structured values never change with the language.
    assert [{k: v for k, v in c.items() if k != 'question'} for c in localized] == \
           [{k: v for k, v in c.items() if k != 'question'} for c in korean]
    assert api.client.get(f'/api/v1/cases/{cid}/results').status_code == 200
    assert api.translator.calls == []


def test_localize_results_keeps_amounts_codes_and_unknown_text():
    data = {'items': [
        {'id': 'netPay', 'title': '실수령액 산술 검사', 'status': 'DIFFERENCE', 'difference': 1200,
         'nextAction': '자료와 확인값을 검토하세요.'},
        {'id': 'HOURLY_WAGE', 'title': '계약·명세서 시급 대조', 'status': 'MATCH', 'nextAction': None},
        {'id': 'FUTURE_CHECK', 'title': '새 항목', 'status': 'MATCH', 'nextAction': '다른 안내'},
    ], 'summary': {'total': 3}}
    out = localize_results(data, 'zh')
    assert [i['title'] for i in out['items']] == ['实发工资算术核对', '合同与工资单时薪核对', '새 항목']
    assert [i['nextAction'] for i in out['items']] == ['请核对资料和已确认的数值。', None, '다른 안내']
    assert out['items'][0]['difference'] == 1200 and out['summary'] == {'total': 3}
    assert data['items'][0]['title'] == '실수령액 산술 검사'
    assert localize_results(data, 'ko') is data


def test_every_catalog_phrase_has_all_languages():
    for key, entry in CATALOG.items():
        assert set(entry) == {'ko', 'en', 'vi', 'zh', 'km'}, key
        assert all(entry.values()), key


def test_translation_is_cached_guarded_and_falls_back_to_korean(tmp_path):
    db = Database(tmp_path / 'db.sqlite3')
    db.initialize()
    translator = FakeTranslator()
    localizer = Localizer(Settings(_env_file=None), db, translator)
    texts = ['통상시급을(를) 확인해 주세요.', '209시간을 자동 적용하지 않습니다.', 'R-01', '']
    assert localizer.translate(texts, 'ko') == texts and translator.calls == []
    first = localizer.translate(texts, 'en')
    assert first == ['[en] 통상시급을(를) 확인해 주세요.', '[en] 209시간을 자동 적용하지 않습니다.', 'R-01', '']
    # Only Korean sentences are sent; codes and empty strings are left alone.
    assert translator.calls == [(texts[:2], 'en')]
    assert localizer.translate(texts, 'en') == first and len(translator.calls) == 1
    translator.broken.add('월급 209시간 기준입니다.')
    assert localizer.translate(['월급 209시간 기준입니다.'], 'vi') == ['월급 209시간 기준입니다.']
    translator.broken.clear()
    # A rejected translation is not cached, so a later attempt can succeed.
    assert localizer.translate(['월급 209시간 기준입니다.'], 'vi') == ['[vi] 월급 209시간 기준입니다.']


def test_translation_without_model_or_after_failure_returns_original(tmp_path):
    db = Database(tmp_path / 'db.sqlite3')
    db.initialize()
    assert Localizer(Settings(_env_file=None, openai_api_key=None), db).translate(['확인해 주세요.'], 'en') == ['확인해 주세요.']

    class Failing:
        calls = 0

        def translate(self, texts, language):
            Failing.calls += 1
            raise TimeoutError

    localizer = Localizer(Settings(_env_file=None), db, Failing())
    assert localizer.translate(['확인해 주세요.'], 'en') == ['확인해 주세요.']
    assert localizer.translate(['확인해 주세요.'], 'en') == ['확인해 주세요.']
    assert Failing.calls == 1  # cooled down instead of retrying every request


def test_fact_guard():
    assert preserves_facts('1,234,000원을 2026-10-03에 받았습니다.', 'Received KRW 1,234,000 on 2026-10-03.')
    assert not preserves_facts('1,234,000원', 'KRW 1,234,500')
    assert not preserves_facts('{{gross}} 입니다.', 'It is gross.')
    assert not preserves_facts('확인', '')


def test_case_questions_are_translated_without_changing_ids(api, monkeypatch):
    wid = api.client.post('/api/v1/workplaces', json={'name': '테스트 사업장'}).json()['data']['workplaceId']
    cid = api.client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'HOURLY',
        'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}).json()['data']['caseId']
    question = {'question_id': 'q-1', 'reason_code': 'CONDITION_MISSING', 'text': '통상시급을(를) 확인해 주세요.',
                'reason': '계산에 필요한 조건이 비어 있어 관련 항목만 계산을 보류했습니다.'}
    with sqlite3.connect(api.app.state.settings.db_path) as con:  # foreign keys are off on a raw connection
        con.execute('''INSERT INTO agent_results(id,job_id,owner_id,case_id,group_id,snapshot_id,version,freshness,
            result_hash,data_json,created_at) VALUES('r-1','j-1',1,?,'g-1','s-1',1,'current','h',?,?)''',
                    (cid, dump({'questions': [question]}), now()))
    monkeypatch.setattr(analysis, 'latest', lambda con, case_id: ({'id': 7, 'data_json': dump({'_agentResultId': 'r-1'})}, None))
    korean = api.client.get(f'/api/v1/cases/{cid}/questions').json()['data']['questions']
    assert korean[0]['question'] == question['text'] and api.translator.calls == []
    api.client.patch('/api/v1/users/me/language', json={'language': 'km'})
    localized = api.client.get(f'/api/v1/cases/{cid}/questions').json()['data']['questions'][0]
    assert localized == {**korean[0], 'question': '[km] ' + question['text'], 'reason': '[km] ' + question['reason']}


def test_chat_model_receives_the_current_language():
    assert '한국어로 답하세요' in chat_instructions('ko')
    english = chat_instructions('en')
    assert '영어(English)로 답하세요' in english and '원문을 그대로' in english
    assert '한국어로 답하세요' in chat_instructions('unknown')


def test_chat_payload_follows_a_language_change_on_the_next_message(tmp_path):
    class Recording:
        def __init__(self):
            self.calls = []

        async def chat(self, payload):
            self.calls.append(payload)
            return {'content': 'answer', 'sources': []}

    model = Recording()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / 'data', workers_enabled=False), ai_client=model)
    with TestClient(app) as client:
        client.post('/api/v1/auth/signup', json={'email': 'c@example.test', 'password': 'secure-test-password', 'name': '채팅'})
        token = client.post('/api/v1/auth/login', json={'email': 'c@example.test', 'password': 'secure-test-password'})
        client.headers['Authorization'] = 'Bearer ' + token.json()['data']['accessToken']
        wid = client.post('/api/v1/workplaces', json={'name': '테스트 사업장'}).json()['data']['workplaceId']
        cid = client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'HOURLY',
            'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}).json()['data']['caseId']
        scope = {'scopeType': 'CASE', 'caseId': cid}
        chat_id = client.post(f'/api/v1/workplaces/{wid}/chats', json={'defaultScope': scope}).json()['data']['chatId']
        path = f'/api/v1/workplaces/{wid}/chats/{chat_id}/messages'
        assert '"status":"COMPLETED"' in client.post(path, json={'content': '질문', 'scope': scope}).text
        client.patch('/api/v1/users/me/language', json={'language': 'zh'})
        assert '"status":"COMPLETED"' in client.post(path, json={'content': '질문 2', 'scope': scope}).text
    assert [call['language'] for call in model.calls] == ['ko', 'zh']
