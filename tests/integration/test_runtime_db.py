"""Real SQLite/files + mapping + agent.run_task + backend finalization.

Only the external model is replaced by FixtureProvider. Digital PDF reading,
calculation rules, checkpoints and PDF/ZIP rendering are real implementations.
Tests that substitute image OCR declare that substitution in their own body.
"""
import io
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader

from agent.config import AgentSettings
from agent.schemas.public.errors import AccessRevoked, ExecutionRevoked, ModelTransientError
from agent.services.hashing import content_hash, result_hash, snapshot_hash
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import analysis_fixture, pdf_bytes
from agent.tests.test_standard_wages import standard_snapshot
from app.db import Database, dump, load, now
from app.integration_store import IntegrationStore, ensure_default_group


def confirmation_from(snapshot):
    """Author-created public inputs; backend supplies trusted identity/evidence."""
    data = snapshot.model_dump(mode='json')
    for key in ('snapshot_id', 'snapshot_hash', 'input_version', 'owner_id', 'workplace_id',
                'case_id', 'period', 'authorized_sources', 'evidence_catalog', 'transaction_allocations',
                'rule_bundle_version', 'support_manifest_version'):
        data.pop(key, None)
    # Rebind authored facts to an authenticated manual confirmation. Source IDs
    # from isolated agent fixtures are never smuggled into backend storage.
    def strip_refs(value):
        if isinstance(value, dict):
            return {k: [] if k == 'evidence_refs' else strip_refs(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip_refs(v) for v in value]
        return value
    return strip_refs(data) | {'subjects_confirmed': True}


class Harness:
    def __init__(self, root):
        from app.agent_service import AgentService
        self.settings = SimpleNamespace(data_dir=root, agent_concurrency=1)
        self.db = Database(root / 'app.sqlite3')
        self.db.initialize()
        self.store = IntegrationStore(self.db, root)
        self.store.initialize()
        self.provider = FixtureProvider()
        self.agent_settings = AgentSettings(retry_delay_seconds=0,
            font_path=Path(__file__).resolve().parents[2]/'assets/fonts/NotoSansKR-Regular.ttf')
        self.service = AgentService(self.db, self.settings, self.agent_settings, provider=self.provider)
        with self.db.connect(write=True) as con:
            con.execute("INSERT INTO users(id,email,name,password_hash) VALUES(1,'synthetic@example.test','합성 근로자','not-for-login')")
            con.execute("INSERT INTO users(id,email,name,password_hash) VALUES(2,'other@example.test','다른 사용자','not-for-login')")
            con.execute("INSERT INTO workplaces(id,user_id,name,employment_status,created_at,updated_at) VALUES(1,1,'합성 사업장','CURRENT',?,?)", (now(), now()))
        self.case()

    def case(self, cid=1, profile='H-STD', period=('2026-04-01','2026-04-30')):
        with self.db.connect(write=True) as con:
            con.execute('INSERT INTO cases(id,workplace_id,pay_type,period_start,period_end,updated_at) VALUES(?,1,?,?,?,?)',
                (cid, 'HOURLY' if profile == 'H-STD' else 'MONTHLY', *period, now()))
            return ensure_default_group(con, cid)

    def group(self, gid, confirmed=None, cid=1):
        with self.db.connect(write=True) as con:
            con.execute('INSERT OR IGNORE INTO comparison_groups(id,case_id,name,created_at,updated_at) VALUES(?,?,?,?,?)',
                (gid, cid, gid, now(), now()))
            if confirmed is not None:
                con.execute('UPDATE comparison_groups SET confirmed_json=? WHERE id=?', (dump(confirmed), gid))
        return gid

    def document(self, data=None, media='application/pdf', cid=None):
        data = data or pdf_bytes(['SYNTHETIC PAYSLIP', 'gross: 2000000 KRW',
            'deduction: 200000 KRW', 'net: 1800000 KRW'])
        with self.db.connect(write=True) as con:
            did = con.execute('''INSERT INTO documents(workplace_id,document_type,file_name,storage_name,media_type,
                sha256,created_at) VALUES(1,'PAYSLIP','synthetic.pdf','pending',?,?,?)''',
                (media, content_hash(data), now())).lastrowid
            name = f'{did}.pdf' if media == 'application/pdf' else f'{did}.png'
            con.execute('UPDATE documents SET storage_name=?,file_name=? WHERE id=?', (name, name, did))
            if cid:
                con.execute('INSERT INTO document_links(case_id,document_id) VALUES(?,?)', (cid, did))
        (self.settings.data_dir / 'uploads' / name).write_bytes(data)
        return did

    def select(self, gid, did):
        from app.integration_store import source_ref
        with self.db.connect(write=True) as con:
            cid = con.execute('SELECT case_id FROM comparison_groups WHERE id=?',(gid,)).fetchone()[0]
            ref = source_ref(con,did,1,cid)
            con.execute('INSERT INTO group_documents(group_id,document_id,document_version,link_version) VALUES(?,?,?,?)',
                        (gid,did,ref.document_version,ref.link_version))

    def row(self, table, key):
        with self.db.connect() as con:
            return dict(con.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone())

    async def execute(self, jid):
        await self.service.execute_job(jid)
        return self.row('agent_jobs', jid)

    async def analysis(self, gid):
        jid = self.service.enqueue_analysis(1, gid)
        job = await self.execute(jid)
        assert job['status'] == 'COMPLETED', job
        with self.db.connect() as con:
            result, _, snapshot = self.store.result(con, 1, job['result_id'])
        return result, snapshot, jid


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / 'integration')


@pytest.mark.parametrize('profile,total', [('H-STD','2270000'),('M-STD','3272727')])
async def test_real_agent_hourly_monthly_persisted_and_hashed(h, profile, total):
    source = standard_snapshot(profile)
    gid = h.group('standard', confirmation_from(source))
    with h.db.connect(write=True) as con:
        con.execute('UPDATE cases SET pay_type=? WHERE id=1', ('HOURLY' if profile == 'H-STD' else 'MONTHLY',))
    result, snap, jid = await h.analysis(gid)
    assert str(result.wage_section.gross_total) == total
    assert result.wage_section.scope_complete
    assert len(result.wage_section.items) == 5
    assert result.case_id == '1'
    assert result.result_hash == result_hash(result)
    from agent.schemas.public.inputs import InputSnapshot
    assert snap['snapshot_hash'] == snapshot_hash(InputSnapshot.model_validate(snap))
    assert result.snapshot_hash == snap['snapshot_hash']
    with h.db.connect() as con:
        assert con.execute('SELECT latest_result_id FROM comparison_groups WHERE id=?',(gid,)).fetchone()[0] == result.result_id
        cp = load(con.execute('SELECT data_json FROM agent_checkpoints WHERE job_id=?',(jid,)).fetchone()[0])
        assert sum(cp['call_budget'].values()) == 0


async def test_missing_wage_conditions_keep_possible_statement_checks(h):
    authored = analysis_fixture()[0].payload.snapshot
    confirmed = confirmation_from(authored)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE cases SET period_start='2026-10-01',period_end='2026-10-31' WHERE id=1")
    h.group('partial', confirmed)
    result, _, _ = await h.analysis('partial')
    rules = {x.rule_id:x for x in result.comparisons}
    assert result.wage_section.gross_total is None
    assert not result.wage_section.scope_complete
    assert rules['R-03'].status == rules['R-04'].status == rules['R-05'].status == 'match'
    assert rules['R-06'].status == 'insufficient'


async def test_alternative_groups_have_independent_inputs_results_and_snapshots(h):
    a = confirmation_from(standard_snapshot('H-STD'))
    b = confirmation_from(standard_snapshot('H-STD'))
    b['confirmed_fields'][0]['value'] = '12000'
    b['wage_conditions']['ordinary_hourly_rate']['value'] = '12000'
    h.group('group-a', a)
    h.group('group-b', b)
    ra, sa, _ = await h.analysis('group-a')
    rb, sb, _ = await h.analysis('group-b')
    assert ra.case_id == rb.case_id == '1'
    assert ra.result_id != rb.result_id and sa['snapshot_id'] != sb['snapshot_id']
    assert ra.wage_section.gross_total == Decimal('2270000')
    assert rb.wage_section.gross_total == Decimal('2724000')
    assert h.row('comparison_groups','group-a')['latest_result_id'] == ra.result_id
    assert h.row('comparison_groups','group-b')['latest_result_id'] == rb.result_id
    with h.db.connect(write=True) as con:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            con.execute("UPDATE agent_snapshots SET data_json='{}' WHERE id=?", (sa['snapshot_id'],))


async def test_document_scope_read_extract_candidate_confirm_calculate(h):
    did = h.document()
    read = await h.execute(h.service.enqueue_read(1,did))
    assert read['status'] == 'COMPLETED', read
    extract = await h.execute(h.service.enqueue_extraction(1,did,document_type='payslip'))
    assert extract['status'] == 'COMPLETED', extract
    assert extract['case_id'] is None
    with h.db.connect() as con:
        ocr = con.execute('SELECT * FROM document_ocr WHERE document_id=?',(did,)).fetchone()
        candidate = con.execute('SELECT * FROM extraction_candidates WHERE document_id=?',(did,)).fetchone()
        assert candidate['ocr_result_id'] == ocr['id']
        assert load(ocr['data_json'])['page_methods'] == ['pdf_text']
        data = load(candidate['data_json'])
    fields = {f['field_id']:f for f in data['fields']}
    assert fields['gross']['normalized_value'] == '2000000'
    confirmed = confirmation_from(analysis_fixture()[0].payload.snapshot)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE cases SET period_start='2026-10-01',period_end='2026-10-31' WHERE id=1")
    for f in confirmed['confirmed_fields']:
        if f['field_id'] in fields:
            f['value'] = fields[f['field_id']]['normalized_value']
    h.group('confirmed', confirmed)
    with h.db.connect(write=True) as con:
        con.execute('INSERT INTO document_links(case_id,document_id) VALUES(1,?)',(did,))
    h.select('confirmed',did)
    result, _, _ = await h.analysis('confirmed')
    assert next(x for x in result.comparisons if x.rule_id == 'R-05').status == 'match'
    assert h.row('extraction_candidates',candidate['id'])['data_json'] == candidate['data_json']


async def test_reextraction_preserves_confirmed_inputs_and_candidate_history(h):
    did = h.document(cid=1)
    h.group('review',confirmation_from(standard_snapshot()))
    h.select('review',did)
    await h.execute(h.service.enqueue_read(1,did))
    first = await h.execute(h.service.enqueue_extraction(1,did,document_type='payslip'))
    before = h.row('comparison_groups','review')['confirmed_json']
    second = await h.execute(h.service.enqueue_extraction(1,did,document_type='payslip'))
    assert first['status'] == second['status'] == 'COMPLETED'
    assert h.row('comparison_groups','review')['confirmed_json'] == before
    assert h.row('comparison_groups','review')['needs_review'] == 1
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM extraction_candidates WHERE document_id=?',(did,)).fetchone()[0] == 2


@pytest.mark.parametrize('mutation',['input','group'])
async def test_queued_snapshot_does_not_replace_latest_after_version_change(h, mutation):
    h.group('stale',confirmation_from(standard_snapshot('H-STD')))
    old,_,_ = await h.analysis('stale')
    jid = h.service.enqueue_analysis(1,'stale')
    with h.db.connect(write=True) as con:
        if mutation == 'input':
            con.execute('UPDATE cases SET input_version=input_version+1 WHERE id=1')
        else:
            con.execute("UPDATE comparison_groups SET version=version+1 WHERE id='stale'")
    job = await h.execute(jid)
    assert job['status'] == 'COMPLETED', job
    assert h.row('agent_results',job['result_id'])['freshness'] == 'stale'
    assert h.row('comparison_groups','stale')['latest_result_id'] == old.result_id


async def test_cancellation_during_model_call_blocks_candidate_and_releases_db(h):
    did = h.document()
    await h.execute(h.service.enqueue_read(1,did))
    jid = h.service.enqueue_extraction(1,did,document_type='payslip')
    class Canceller(FixtureProvider):
        async def invoke(self, operation, data):
            # A second writer succeeds during the awaited paid call, proving no
            # original writer transaction is retained across run_task.
            with h.db.connect(write=True) as con:
                con.execute("UPDATE workplaces SET name='수정 가능' WHERE id=1")
            h.store.cancel(1,jid)
            return await super().invoke(operation,data)
    h.service.provider = Canceller()
    job = await h.execute(jid)
    assert job['status'] == 'CANCELLED'
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM extraction_candidates WHERE job_id=?',(jid,)).fetchone()[0] == 0


async def test_checkpoint_budget_survives_retry_without_additional_paid_calls(h):
    did = h.document()
    await h.execute(h.service.enqueue_read(1,did))
    class Failing:
        calls = 0
        async def invoke(self, operation, data):
            self.calls += 1
            raise ModelTransientError()
    provider = Failing()
    h.service.provider = provider
    jid = h.service.enqueue_extraction(1,did,document_type='payslip')
    failed = await h.execute(jid)
    assert failed['status'] == 'FAILED' and provider.calls == 3
    h.store.retry(1,jid)
    again = await h.execute(jid)
    assert again['status'] == 'FAILED' and again['error_code'] == 'MODEL_BUDGET_EXHAUSTED'
    assert provider.calls == 3


async def test_restart_revokes_old_execution_and_keeps_one_result(h):
    from app.agent_ports import DatabaseAgentPorts
    from agent.schemas.public.tasks import JobContext
    from datetime import datetime, UTC
    h.group('restart',confirmation_from(standard_snapshot('H-STD')))
    jid = h.service.enqueue_analysis(1,'restart')
    claim = h.store.claim(jid)
    context = JobContext(job_id=jid,run_id=claim['run_id'],execution_token=claim['token'],owner_id='1',
        case_id='1',task_kind='analyze_case',idempotency_key=claim['idempotency_key'],requested_at=datetime.now(UTC))
    h.store.recover()
    with pytest.raises(ExecutionRevoked):
        await DatabaseAgentPorts(h.store).check_control(context)
    job = await h.execute(jid)
    assert job['status'] == 'COMPLETED' and job['attempts'] == 2
    await h.service.execute_job(jid)
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM agent_results WHERE job_id=?',(jid,)).fetchone()[0] == 1


async def test_other_user_cannot_read_document_result_or_enqueue(h):
    did = h.document()
    with pytest.raises(AccessRevoked):
        h.service.enqueue_read(2,did)
    h.group('private',confirmation_from(standard_snapshot('H-STD')))
    result,_,_ = await h.analysis('private')
    from app.errors import ApiError
    with h.db.connect() as con:
        with pytest.raises(ApiError):
            h.store.result(con,2,result.result_id)


async def test_report_uses_pinned_old_snapshot_and_download_revocation(h):
    h.group('report',confirmation_from(standard_snapshot('H-STD')))
    did = h.document(cid=1)
    h.select('report',did)
    result, snap, _ = await h.analysis('report')
    explanation = await h.execute(h.service.enqueue_explanation(1,result.result_id))
    assert explanation['status'] == 'COMPLETED', explanation
    with h.db.connect(write=True) as con:
        con.execute("UPDATE comparison_groups SET version=version+1,confirmed_json='{}' WHERE id='report'")
        con.execute("UPDATE cases SET period_start='2026-05-01',period_end='2026-05-31',input_version=input_version+1 WHERE id=1")
    report = await h.execute(h.service.enqueue_report(1,result.result_id,explanation['id'],selected_document_ids=[did]))
    assert report['status'] == 'COMPLETED', report
    output = load(report['output_json'])
    assert output['result_id'] == result.result_id and output['result_version'] == result.result_version
    artifacts = output['artifacts']
    pdf = next(x for x in artifacts if x['mime_type'] == 'application/pdf')
    path,_ = h.store.artifact(1,pdf['artifact_id'])
    text = '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
    assert '2,270,000' in text and '2026-04-01' in text and '2026-04-30' in text
    assert '2026-05-01' not in text
    assert snap['snapshot_id'] == result.snapshot_id
    with h.db.connect(write=True) as con:
        con.execute('DELETE FROM document_links WHERE case_id=1 AND document_id=?',(did,))
    with pytest.raises(AccessRevoked):
        h.store.artifact(1,pdf['artifact_id'])


async def test_image_keeps_original_and_records_derived_pdf_hash(h):
    stream = io.BytesIO()
    Image.new('RGB',(40,40),'white').save(stream,format='PNG')
    original = stream.getvalue()
    did = h.document(original,media='image/png')
    async def synthetic_image_ocr(data, settings, control=None):
        # Bounded substitution: only bitmap OCR recognition is replaced.
        assert data.startswith(b'%PDF-')
        return {'pages':['Synthetic image payroll gross: 100 KRW'],'page_count':1,'image_pages':[True],
            'ocr_pages':[{'text':'Synthetic image payroll gross: 100 KRW','lines':[]}],
            'page_methods':['python_ocr'],'source_method':'python_ocr'}
    h.service.recognizer = synthetic_image_ocr
    read = await h.execute(h.service.enqueue_read(1,did))
    assert read['status'] == 'COMPLETED',read
    with h.db.connect() as con:
        derivative = con.execute('SELECT * FROM document_derivatives WHERE document_id=?',(did,)).fetchone()
    assert derivative['original_hash'] == content_hash(original)
    derived = (h.settings.data_dir/'derived'/derivative['storage_name']).read_bytes()
    assert derivative['content_hash'] == content_hash(derived)
    assert (h.settings.data_dir/'uploads'/f'{did}.png').read_bytes() == original


async def test_case_allocation_is_reused_by_groups_without_consuming_twice(h):
    from app.routers.payments import insert_allocation
    from app.errors import ApiError
    confirmed = confirmation_from(analysis_fixture()[0].payload.snapshot)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE cases SET period_start='2026-10-01',period_end='2026-10-31' WHERE id=1")
        tid = con.execute("INSERT INTO payment_transactions(workplace_id,payment_method,transaction_date,amount,source,confirmation_status) VALUES(1,'BANK_TRANSFER','2026-11-05',1800000,'USER_INPUT','USER_CONFIRMED')").lastrowid
        insert_allocation(con,1,tid,1800000)
    confirmed['selected_transaction_ids'] = [tid]
    h.group('alternative-1',confirmed)
    h.group('alternative-2',confirmed)
    for gid in ('alternative-1','alternative-2'):
        result,_,_ = await h.analysis(gid)
        r6 = next(x for x in result.comparisons if x.rule_id == 'R-06')
        assert r6.status == 'match' and r6.rhs == Decimal('1800000')
    h.case(2)
    with h.db.connect(write=True) as con:
        assert con.execute('SELECT sum(allocated_amount) FROM allocations WHERE transaction_id=?',(tid,)).fetchone()[0] == 1800000
        with pytest.raises(ApiError) as error:
            insert_allocation(con,2,tid,1)
        assert error.value.code == 'ALLOCATION_EXCEEDS_TRANSACTION'


async def test_cash_unconfirmed_and_duplicate_transactions_not_bank_total(h):
    confirmed = confirmation_from(analysis_fixture()[0].payload.snapshot)
    ids = []
    with h.db.connect(write=True) as con:
        con.execute("UPDATE cases SET period_start='2026-10-01',period_end='2026-10-31' WHERE id=1")
        for method,date,amount,state in [
            ('BANK_TRANSFER','2026-11-01',1000000,'USER_CONFIRMED'),
            ('CASH','2026-11-02',100000,'USER_CONFIRMED'),
            ('BANK_TRANSFER','2026-11-03',200000,'USER_REPORTED'),
            ('BANK_TRANSFER','2026-11-04',50000,'USER_CONFIRMED'),
            ('BANK_TRANSFER','2026-11-04',50000,'USER_CONFIRMED')]:
            tid = con.execute("INSERT INTO payment_transactions(workplace_id,payment_method,transaction_date,amount,source,confirmation_status) VALUES(1,?,?,?,'USER_INPUT',?)",(method,date,amount,state)).lastrowid
            con.execute('INSERT INTO allocations(case_id,transaction_id,allocated_amount) VALUES(1,?,?)',(tid,amount))
            ids.append(tid)
    confirmed['selected_transaction_ids'] = ids
    h.group('payments',confirmed)
    result,snapshot,_ = await h.analysis('payments')
    rows = snapshot['transaction_allocations']
    allowed = [x for x in rows if x['kind']=='bank' and x['confirmed'] and not x['duplicate']]
    assert sum(Decimal(x['amount']) for x in allowed) == Decimal('1000000')
    r6 = next(x for x in result.comparisons if x.rule_id == 'R-06')
    # Unresolved duplicate scope blocks a definitive comparison; it is never
    # promoted to a bank payment total by cash/unconfirmed/duplicate rows.
    assert r6.status in ('insufficient','needs_review')
    assert r6.rhs is None or r6.rhs <= Decimal('1000000')


@pytest.mark.parametrize('revocation',['cancel','delete_case','delete_source','unlink','owner','execution'])
async def test_revocation_at_final_commit_blocks_publication(h,monkeypatch,revocation):
    h.group('fenced',confirmation_from(standard_snapshot('H-STD')))
    did = h.document(cid=1)
    h.select('fenced',did)
    jid = h.service.enqueue_analysis(1,'fenced')
    actual_finalize = h.service.finalize
    def revoke_then_finalize(context,outcome):
        assert outcome.status == 'completed',outcome
        with h.db.connect(write=True) as con:
            if revocation == 'cancel':
                con.execute('UPDATE agent_jobs SET cancel_requested=1 WHERE id=?',(jid,))
            elif revocation == 'delete_case':
                con.execute('UPDATE cases SET deleted_at=? WHERE id=1',(now(),))
            elif revocation == 'delete_source':
                con.execute('UPDATE documents SET deleted_at=? WHERE id=?',(now(),did))
            elif revocation == 'unlink':
                con.execute('DELETE FROM document_links WHERE case_id=1 AND document_id=?',(did,))
            elif revocation == 'owner':
                con.execute('UPDATE workplaces SET user_id=2 WHERE id=1')
            else:
                con.execute("UPDATE agent_jobs SET run_id='newer-run',execution_hash='revoked' WHERE id=?",(jid,))
        return actual_finalize(context,outcome)
    monkeypatch.setattr(h.service,'finalize',revoke_then_finalize)
    await h.execute(jid)
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM agent_results WHERE job_id=?',(jid,)).fetchone()[0] == 0
        assert con.execute("SELECT latest_result_id FROM comparison_groups WHERE id='fenced'").fetchone()[0] is None
        assert con.execute('SELECT status FROM agent_jobs WHERE id=?',(jid,)).fetchone()[0] != 'COMPLETED'


async def test_input_change_after_real_agent_returns_keeps_old_result_stale(h,monkeypatch):
    h.group('late-edit',confirmation_from(standard_snapshot('H-STD')))
    first,_,_ = await h.analysis('late-edit')
    jid = h.service.enqueue_analysis(1,'late-edit')
    actual_finalize = h.service.finalize
    def edit_then_finalize(context,outcome):
        with h.db.connect(write=True) as con:
            con.execute('UPDATE cases SET input_version=input_version+1 WHERE id=1')
            con.execute("UPDATE comparison_groups SET version=version+1 WHERE id='late-edit'")
        return actual_finalize(context,outcome)
    monkeypatch.setattr(h.service,'finalize',edit_then_finalize)
    job = await h.execute(jid)
    assert job['status']=='COMPLETED'
    assert h.row('agent_results',job['result_id'])['freshness']=='stale'
    assert h.row('comparison_groups','late-edit')['latest_result_id']==first.result_id


async def test_analysis_idempotency_replay_and_conflict(h):
    from app.errors import ApiError
    h.group('idempotent',confirmation_from(standard_snapshot('H-STD')))
    one = h.service.enqueue_analysis(1,'idempotent',idempotency_key='one-calculation')
    two = h.service.enqueue_analysis(1,'idempotent',idempotency_key='one-calculation')
    assert one==two
    await h.execute(one)
    assert h.service.enqueue_analysis(1,'idempotent',idempotency_key='one-calculation')==one
    with h.db.connect(write=True) as con:
        con.execute("UPDATE comparison_groups SET version=version+1 WHERE id='idempotent'")
    with pytest.raises(ApiError) as error:
        h.service.enqueue_analysis(1,'idempotent',idempotency_key='one-calculation')
    assert error.value.code=='IDEMPOTENCY_KEY_REUSED'


async def test_pdf_supplement_retains_a_and_never_confirms_automatically(h):
    did = h.document()
    await h.execute(h.service.enqueue_read(1,did))
    class UncertainPrimary(FixtureProvider):
        async def invoke(self,operation,data):
            response,usage = await super().invoke(operation,data)
            response['fields'][0]['quote']='Wrong synthetic quote to require manual review'
            return response,usage
    h.service.provider=UncertainPrimary()
    a = await h.execute(h.service.enqueue_extraction(1,did,document_type='payslip'))
    a_output = load(a['output_json'])
    a_id = a_output['candidate_id']
    candidates = a_output['candidate']['fields']
    missing = next(f for f in candidates if f['validation_status']=='needs_review')
    class Supplement(FixtureProvider):
        async def invoke(self,operation,data):
            assert operation=='extract_pdf'
            assert data['pdf_bytes'].startswith(b'%PDF-')
            return {'document_type':'payslip','fields':[],'issues':[]},{}
    h.service.provider=Supplement()
    b = await h.execute(h.service.enqueue_extraction(1,did,method='pdf_supplement',
        target_fields=[{'field_id':missing['field_id'],'page':1}]))
    assert b['status']=='COMPLETED',b
    b_id=load(b['output_json'])['candidate_id']
    assert h.row('extraction_candidates',b_id)['base_candidate_id']==a_id
    assert h.row('extraction_candidates',a_id)['data_json']==dump(a_output['candidate'])
    with h.db.connect() as con:
        assert con.execute("SELECT count(*) FROM fields WHERE document_id=? AND status='CONFIRMED'",(did,)).fetchone()[0]==0


async def test_image_report_zip_contains_uploaded_bytes_and_derived_lineage(h):
    import json
    import zipfile
    stream=io.BytesIO()
    Image.new('RGB',(40,40),'white').save(stream,format='PNG')
    original=stream.getvalue()
    did=h.document(original,media='image/png',cid=1)
    async def image_ocr(data,settings,control=None):
        return {'pages':['Synthetic image payroll gross: 100 KRW'],'page_count':1,'image_pages':[True],
            'ocr_pages':[{'text':'Synthetic image payroll gross: 100 KRW','lines':[]}],
            'page_methods':['python_ocr'],'source_method':'python_ocr'}
    h.service.recognizer=image_ocr
    read=await h.execute(h.service.enqueue_read(1,did))
    assert read['status']=='COMPLETED',read
    h.group('image-report',confirmation_from(standard_snapshot('H-STD')))
    h.select('image-report',did)
    result,_,_=await h.analysis('image-report')
    explanation=await h.execute(h.service.enqueue_explanation(1,result.result_id))
    report=await h.execute(h.service.enqueue_report(1,result.result_id,explanation['id'],selected_document_ids=[did]))
    assert report['status']=='COMPLETED',report
    artifact=next(a for a in load(report['output_json'])['artifacts'] if a['mime_type']=='application/zip')
    path,_=h.store.artifact(1,artifact['artifact_id'])
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read('source_manifest.json'))
        assert len(manifest)==1
        entry=manifest[0]
        assert entry['original_hash']==content_hash(original)
        assert entry['processing_pdf_hash']!=entry['original_hash']
        assert entry['archive_name'].endswith('.png')
        assert archive.read(entry['archive_name'])==original


async def test_file_changed_during_model_call_cannot_publish_candidate(h):
    did=h.document()
    await h.execute(h.service.enqueue_read(1,did))
    class TamperingProvider(FixtureProvider):
        async def invoke(self,operation,data):
            response=await super().invoke(operation,data)
            path=h.settings.data_dir/'uploads'/h.row('documents',did)['storage_name']
            path.write_bytes(pdf_bytes(['DIFFERENT DOCUMENT','gross: 100 KRW']))
            return response
    h.service.provider=TamperingProvider()
    jid=h.service.enqueue_extraction(1,did,document_type='payslip')
    job=await h.execute(jid)
    assert job['status']=='FAILED',job
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM extraction_candidates WHERE job_id=?',(jid,)).fetchone()[0]==0


async def test_report_failure_preserves_existing_calculation(h):
    h.group('report-failure',confirmation_from(standard_snapshot('H-STD')))
    result,_,_=await h.analysis('report-failure')
    explanation=await h.execute(h.service.enqueue_explanation(1,result.result_id))
    h.agent_settings.font_path=h.settings.data_dir/'missing-font.ttf'
    jid=h.service.enqueue_report(1,result.result_id,explanation['id'])
    failed=await h.execute(jid)
    assert failed['status']=='FAILED',failed
    with h.db.connect() as con:
        preserved,_,_=h.store.result(con,1,result.result_id)
        assert preserved.result_hash==result.result_hash
        assert preserved.wage_section.gross_total==Decimal('2270000')
        assert con.execute("SELECT count(*) FROM agent_artifacts WHERE job_id=? AND status='PUBLISHED'",(jid,)).fetchone()[0]==0
    assert h.row('comparison_groups','report-failure')['latest_result_id']==result.result_id
