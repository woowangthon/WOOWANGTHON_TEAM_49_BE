"""Extracted field display names follow the user's language; values never change."""
import sqlite3
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database, dump, now
from app.field_labels import KOREAN_COLUMNS, KOREAN_FIELDS, _COLUMNS, _FIELDS, _ROWS, field_label, localize_fields
from app.i18n import Localizer
from app.main import create_app


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def translate(self, texts, language):
        self.calls.append(list(texts))
        return [f'[{language}] ' + t for t in texts]


def test_every_known_field_and_table_column_has_every_language():
    assert set(KOREAN_FIELDS) <= set(_FIELDS), set(KOREAN_FIELDS) - set(_FIELDS)
    for row, columns in KOREAN_COLUMNS.items():
        assert row in _ROWS
        assert set(columns) <= set(_COLUMNS[row]), (row, set(columns) - set(_COLUMNS[row]))
    for names in [*_FIELDS.values(), *_ROWS.values(), *(c for cols in _COLUMNS.values() for c in cols.values())]:
        assert len(names) == 4 and all(names)


@pytest.mark.parametrize('language,expected', [
    ('ko', '출근 시각'), ('en', 'Start time'), ('vi', 'Giờ vào làm'), ('zh', '上班时间'), ('km', 'ម៉ោងចូលធ្វើការ')])
def test_known_field_codes_get_a_name_in_each_language(language, expected):
    assert field_label('work_start', language) == expected


def test_table_row_fields_keep_their_row_number():
    assert field_label('transaction_3_deposit_amount', 'ko') == '거래 내역 3 · 입금액 (원)'
    assert field_label('transaction_3_deposit_amount', 'en') == 'Transaction 3 · Deposit amount (KRW)'
    assert field_label('work_12_start', 'zh') == '每日工作记录 12 · 上班'
    assert field_label('deduction', 'en') == 'Total deductions (KRW)'
    assert field_label('deduction_2_amount', 'vi') == 'Khoản khấu trừ 2 · Số tiền (KRW)'
    assert field_label('transaction_1_unknown_column', 'en') is None
    assert field_label('extra_probation', 'en') is None


def test_ai_named_extra_items_are_translated_and_values_untouched(tmp_path):
    translator = FakeTranslator()
    db = Database(tmp_path / 'db.sqlite3')
    db.initialize()
    localizer = Localizer(Settings(_env_file=None), db, translator)
    fields = [
        {'key': 'hourlyWage', 'agentKey': 'contract_rate', 'label': 'contract_rate', 'value': '10,030원'},
        {'key': 'extra_probation', 'label': '수습기간', 'value': '3개월'},
        {'key': 'extra_custom', 'label': 'Probation', 'value': 'x'},
    ]
    out = localize_fields(fields, 'en', localizer)
    assert [f['label'] for f in out] == ['Wage amount', '[en] 수습기간', 'Probation']
    assert [f['value'] for f in out] == ['10,030원', '3개월', 'x']
    assert fields[0]['label'] == 'contract_rate'
    korean = localize_fields(fields, 'ko', localizer)
    assert [f['label'] for f in korean] == ['임금 금액', '수습기간', 'Probation']
    assert len(translator.calls) == 1


def test_case_document_fields_endpoint_uses_the_saved_language(tmp_path):
    translator = FakeTranslator()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / 'data', workers_enabled=False),
                     translation_client=translator)
    with TestClient(app) as client:
        client.post('/api/v1/auth/signup', json={'email': 'f@example.test', 'password': 'secure-test-password', 'name': '필드'})
        token = client.post('/api/v1/auth/login', json={'email': 'f@example.test', 'password': 'secure-test-password'})
        client.headers['Authorization'] = 'Bearer ' + token.json()['data']['accessToken']
        wid = client.post('/api/v1/workplaces', json={'name': '테스트 사업장'}).json()['data']['workplaceId']
        cid = client.post('/api/v1/cases', json={'workplaceId': wid, 'payType': 'HOURLY',
            'periodStart': '2026-10-01', 'periodEnd': '2026-10-31'}).json()['data']['caseId']
        with sqlite3.connect(app.state.settings.db_path) as con:
            did = con.execute('''INSERT INTO documents(workplace_id,document_type,file_name,storage_name,media_type,
                sha256,page_count,processing_status,progress,created_at) VALUES(?,'CONTRACT','c.pdf','u.pdf',
                'application/pdf','h',1,'READY',100,?)''', (wid, now())).lastrowid
            con.execute('INSERT INTO document_links(case_id,document_id) VALUES(?,?)', (cid, did))
            for key, label, value in [('work_start', 'work_start', '09:00'), ('employer_name', 'employer_name', '한빛정밀'),
                                      ('extra_probation', '수습기간', '3개월')]:
                con.execute('''INSERT INTO fields(document_id,key,label,value_json,value_type,status)
                    VALUES(?,?,?,?,'TEXT','UNCONFIRMED')''', (did, key, label, dump(value)))
        path = f'/api/v1/cases/{cid}/documents/{did}/fields'
        korean = client.get(path).json()['data']['fields']
        assert [(f['label'], f['value']) for f in korean] == [('출근 시각', '09:00'), ('사업장명', '한빛정밀'), ('수습기간', '3개월')]
        assert translator.calls == []
        client.patch('/api/v1/users/me/language', json={'language': 'vi'})
        vietnamese = client.get(path).json()['data']['fields']
        assert [(f['label'], f['value']) for f in vietnamese] == [
            ('Giờ vào làm', '09:00'), ('Tên nơi làm việc', '한빛정밀'), ('[vi] 수습기간', '3개월')]
        assert [f['key'] for f in vietnamese] == [f['key'] for f in korean]
