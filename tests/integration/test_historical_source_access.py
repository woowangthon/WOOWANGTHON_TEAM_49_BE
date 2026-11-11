"""Link metadata edits preserve history without relaxing execution/source fencing."""
import pytest

from agent.schemas.public.errors import AccessRevoked
from agent.tests.test_standard_wages import standard_snapshot
from app.db import load, now
from app.engine import results, schedule, snapshot_accessible
from app.integration_store import check_access, check_historical_access, source_ref
from test_runtime_db import Harness, confirmation_from


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path / 'historical-source-access')


def linked_document(h):
    did = h.document(cid=1)
    with h.db.connect(write=True) as con:
        con.execute("UPDATE document_links SET purpose='payroll' WHERE document_id=?", (did,))
    return did


def edit_link(h, did, edit):
    """Mirror the document-links PATCH, including normal stale invalidation."""
    with h.db.connect(write=True) as con:
        if edit == 'same_value':
            con.execute("UPDATE document_links SET purpose='payroll' WHERE document_id=?", (did,))
        elif edit == 'period':
            con.execute("UPDATE document_links SET applicable_from='2026-03-01',applicable_to='2026-04-30' WHERE document_id=?", (did,))
        else:
            con.execute("UPDATE document_links SET purpose='context' WHERE document_id=?", (did,))
        con.execute('UPDATE document_links SET link_version=link_version+1 WHERE document_id=?', (did,))
        schedule(con, 1)


async def completed_report(h, result_id, did):
    explanation = await h.execute(h.service.enqueue_explanation(1, result_id))
    assert explanation['status'] == 'COMPLETED', explanation
    report = await h.execute(h.service.enqueue_report(1, result_id, explanation['id'], selected_document_ids=[did]))
    assert report['status'] == 'COMPLETED', report
    pdf = next(item for item in load(report['output_json'])['artifacts'] if item['mime_type'] == 'application/pdf')
    return explanation, report, pdf['artifact_id']


@pytest.mark.parametrize('edit', ['same_value', 'period', 'purpose'])
async def test_link_metadata_edit_keeps_stale_result_pdf_and_pinned_followup_tasks(h, edit):
    did = linked_document(h)
    gid = h.group('default-1', confirmation_from(standard_snapshot('H-STD')))
    h.select(gid, did)
    result, original_snapshot, analysis_job = await h.analysis(gid)
    explanation, report, pdf_id = await completed_report(h, result.result_id, did)
    original_pdf = h.store.artifact(1, pdf_id)[0].read_bytes()
    original_result_json = h.row('agent_results', result.result_id)['data_json']

    edit_link(h, did, edit)

    with h.db.connect() as con:
        historical, row, snapshot = h.store.result(con, 1, result.result_id)
        assert historical.freshness == 'stale'
        assert snapshot == original_snapshot
        assert snapshot_accessible(con, row)
        assert results(con, 1)['result']['result_id'] == result.result_id
        assert results(con, 1)['freshness']['overall'] == 'STALE'
    assert h.row('agent_results', result.result_id)['data_json'] == original_result_json
    assert h.store.artifact(1, pdf_id)[0].read_bytes() == original_pdf
    for jid in (analysis_job, explanation['id'], report['id']):
        assert h.store.job(1, jid)['status'] == 'COMPLETED'

    # Both fresh follow-up tasks and their source reads are bound to the old
    # committed result; today's link metadata cannot rewrite their snapshot.
    _, new_report, new_pdf = await completed_report(h, result.result_id, did)
    assert load(new_report['output_json'])['result_id'] == result.result_id
    assert h.store.artifact(1, new_pdf)[0].read_bytes().startswith(b'%PDF-')


@pytest.mark.parametrize('change', ['unlink', 'relink', 'version', 'hash', 'owner', 'workplace', 'delete'])
def test_historical_permission_keeps_document_identity_and_revocation_checks(h, change):
    did = linked_document(h)
    with h.db.connect() as con:
        frozen = source_ref(con, did, 1, 1)
    with h.db.connect(write=True) as con:
        if change in ('unlink', 'relink'):
            con.execute('DELETE FROM document_links WHERE document_id=?', (did,))
            if change == 'relink':
                con.execute('INSERT INTO document_links(case_id,document_id) VALUES(1,?)', (did,))
        elif change == 'version':
            con.execute('UPDATE documents SET document_version=document_version+1 WHERE id=?', (did,))
        elif change == 'hash':
            con.execute('UPDATE documents SET sha256=? WHERE id=?', ('0' * 64, did))
        elif change == 'owner':
            con.execute('UPDATE workplaces SET user_id=2 WHERE id=1')
        elif change == 'workplace':
            con.execute("INSERT INTO workplaces(id,user_id,name,employment_status,created_at,updated_at) VALUES(2,1,'Other','CURRENT',?,?)", (now(), now()))
            con.execute('UPDATE documents SET workplace_id=2 WHERE id=?', (did,))
        else:
            con.execute('UPDATE documents SET deleted_at=? WHERE id=?', (now(), did))
    with h.db.connect() as con:
        with pytest.raises(AccessRevoked):
            check_historical_access(con, 1, 1, 'default-1', [frozen])


async def test_metadata_edit_does_not_relax_analysis_execution_or_checkpoint_writes(h):
    did = linked_document(h)
    gid = h.group('strict', confirmation_from(standard_snapshot('H-STD')))
    h.select(gid, did)
    jid = h.service.enqueue_analysis(1, gid)
    claim = h.store.claim(jid)
    context = h.service.context(claim)
    with h.db.connect() as con:
        frozen = source_ref(con, did, 1, 1)
    edit_link(h, did, 'period')
    with h.db.connect() as con:
        check_historical_access(con, 1, 1, gid, [frozen])
        with pytest.raises(AccessRevoked):
            check_access(con, 1, 1, gid, [frozen])
    with pytest.raises(AccessRevoked):
        await h.service.ports.load_checkpoint(context, 'unused')
    await h.service._execute(claim)
    job = h.row('agent_jobs', jid)
    assert job['status'] == 'CANCELLED' and job['error_code'] == 'ACCESS_REVOKED'
    assert job['result_id'] is None
    with h.db.connect() as con:
        assert con.execute('SELECT count(*) FROM agent_results WHERE job_id=?', (jid,)).fetchone()[0] == 0


async def test_pinned_result_task_cannot_read_an_unrelated_source(h):
    did = linked_document(h)
    gid = h.group('pinned', confirmation_from(standard_snapshot('H-STD')))
    h.select(gid, did)
    result, _, _ = await h.analysis(gid)
    other = h.document(cid=1)
    job = h.service.enqueue_explanation(1, result.result_id)
    claimed = h.store.claim(job)
    context = h.service.context(claimed)
    with h.db.connect() as con:
        unrelated = source_ref(con, other, 1, 1)
    with pytest.raises(AccessRevoked):
        await h.service.ports.check_access(context, [unrelated])


async def test_historical_report_still_rejects_modified_file_bytes(h):
    did = linked_document(h)
    gid = h.group('file-integrity', confirmation_from(standard_snapshot('H-STD')))
    h.select(gid, did)
    result, _, _ = await h.analysis(gid)
    explanation = await h.execute(h.service.enqueue_explanation(1, result.result_id))
    assert explanation['status'] == 'COMPLETED'
    edit_link(h, did, 'period')
    report_job = h.service.enqueue_report(1, result.result_id, explanation['id'], selected_document_ids=[did])
    document = h.row('documents', did)
    (h.settings.data_dir / 'uploads' / document['storage_name']).write_bytes(b'changed uploaded bytes')
    failed = await h.execute(report_job)
    assert failed['status'] == 'FAILED' and failed['error_code'] == 'INPUT_INTEGRITY_INVALID'
    with h.db.connect() as con:
        assert con.execute("SELECT count(*) FROM agent_artifacts WHERE job_id=? AND status='PUBLISHED'", (report_job,)).fetchone()[0] == 0
