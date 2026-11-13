"""Current-input publication, immutable history and read-only polling."""
from app.compatibility import KINDS, fingerprint, prepare_snapshot, schedule
from test_review_policy import api as api_fixture, seed
from test_legacy_contract_restored import request, drain

api = api_fixture


def test_partial_current_recompute_and_immutable_history(api):
    client, app, wid, cid = api
    seed(app, wid, cid, 'PAYSLIP', [('net', 900, 'CONFIRMED')])
    with app.state.db.connect(write=True) as con:
        con.execute('UPDATE cases SET input_version=63 WHERE id=?', (cid,))
        schedule(con, cid, increment=False, delay=0)
    drain(app)
    result = request(client, 'GET', f'/cases/{cid}/results')
    assert result['freshness']['overall'] == 'CURRENT'
    with app.state.db.connect(write=True) as con:
        assert con.execute('SELECT status FROM jobs WHERE case_id=? ORDER BY created_at DESC LIMIT 1', (cid,)).fetchone()[0] == 'PARTIAL'
        # A previous fingerprint policy or changed dependency can stale only one check.
        con.execute("UPDATE snapshots SET fingerprint='old-policy' WHERE case_id=? AND kind='depositComparison'", (cid,))
        history = [tuple(r) for r in con.execute('SELECT * FROM snapshots WHERE case_id=? ORDER BY id', (cid,))]
        groups = [tuple(r) for r in con.execute('SELECT * FROM comparison_groups WHERE case_id=? ORDER BY id', (cid,))]
        count = con.execute('SELECT count(*) FROM jobs').fetchone()[0]
    for _ in range(3):
        status = request(client, 'GET', f'/cases/{cid}/recompute-status')
        assert status['results']['depositComparison']['freshness'] == 'STALE'
        assert status['results']['calculation']['freshness'] == 'CURRENT'
    with app.state.db.connect() as con:
        assert count == con.execute('SELECT count(*) FROM jobs').fetchone()[0]
        assert groups == [tuple(r) for r in con.execute('SELECT * FROM comparison_groups WHERE case_id=? ORDER BY id', (cid,))]
    request(client, 'POST', f'/cases/{cid}/recompute', {'expectedInputVersion': 63}, 202, headers={'Idempotency-Key': 'repair'})
    drain(app)
    assert request(client, 'GET', f'/cases/{cid}/results')['freshness']['overall'] == 'CURRENT'
    with app.state.db.connect() as con:
        assert history == [tuple(con.execute('SELECT * FROM snapshots WHERE id=?', (r[0],)).fetchone()) for r in history]


def test_prepare_does_not_change_input_versions(api):
    _, app, _, cid = api
    with app.state.db.connect(write=True) as con:
        prepare_snapshot(con, cid, 'first')
        before = dict(con.execute('SELECT * FROM comparison_groups WHERE id=?', (f'compat-{cid}',)).fetchone())
        fps = {k: fingerprint(con, cid, k) for k in KINDS}
        prepare_snapshot(con, cid, 'second')
        after = dict(con.execute('SELECT * FROM comparison_groups WHERE id=?', (f'compat-{cid}',)).fetchone())
        assert before == after
        assert fps == {k: fingerprint(con, cid, k) for k in KINDS}


def test_repeat_post_reuses_active_job_and_preserves_conflict(api):
    client, app, _, cid = api
    body = {'expectedInputVersion': 1}
    a = request(client, 'POST', f'/cases/{cid}/recompute', body, 202, headers={'Idempotency-Key': 'a'})
    b = request(client, 'POST', f'/cases/{cid}/recompute', body, 202, headers={'Idempotency-Key': 'b'})
    assert a['jobId'] == b['jobId']
    with app.state.db.connect(write=True) as con:
        schedule(con, cid)
    response = client.post(f'/api/v1/cases/{cid}/recompute', json=body, headers={'Idempotency-Key': 'c'})
    assert response.status_code == 409


def test_obsolete_failed_job_does_not_mark_new_input_failed(api):
    client, app, _, cid = api
    with app.state.db.connect(write=True) as con:
        mutation = schedule(con, cid, increment=False)
        con.execute("UPDATE jobs SET status='FAILED',error_code='TEST_FAILURE' WHERE id=?", (mutation['recompute']['jobId'],))
        con.execute('UPDATE cases SET input_version=63 WHERE id=?', (cid,))
    status = request(client, 'GET', f'/cases/{cid}/recompute-status')
    assert all(v['freshness'] == 'STALE' for v in status['results'].values())
    assert status['latestJob']['errorCode'] == 'TEST_FAILURE'


def test_default_group_mode_is_a_fingerprint_dependency(api):
    _, app, _, cid = api
    with app.state.db.connect(write=True) as con:
        con.execute('UPDATE comparison_groups SET confirmed_json=?,needs_review=0 WHERE id=?', ('{"profile":"H-STD"}', f'default-{cid}'))
        before = {k: fingerprint(con, cid, k) for k in KINDS}
        con.execute('UPDATE comparison_groups SET needs_review=1 WHERE id=?', (f'default-{cid}',))
        assert all(before[k] != fingerprint(con, cid, k) for k in KINDS)


def test_group_changed_during_execution_is_superseded(api):
    from app.compatibility import complete_job
    from app.db import dump
    _, app, _, cid = api
    with app.state.db.connect(write=True) as con:
        mutation = schedule(con, cid, increment=False)
        jid = mutation['recompute']['jobId']
        con.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (jid,))
        metadata = {'legacyJobId': jid, 'fingerprints': {k: fingerprint(con, cid, k) for k in KINDS}}
        con.execute('UPDATE comparison_groups SET confirmed_json=?,needs_review=0 WHERE id=?', ('{"profile":"H-STD"}', f'default-{cid}'))
        complete_job(con, {'metadata_json': dump(metadata)}, outcome={})
        assert con.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()[0] == 'SUPERSEDED'


def test_detail_scope_does_not_stale_unrelated_checks(api):
    from app.db import dump
    client, app, wid, cid = api
    did, ids = seed(app, wid, cid, 'PAYSLIP', [
        ('net', 900, 'CONFIRMED'), ('earning_1_amount', 1000, 'UNCONFIRMED'),
        ('earning_1_label', 'base', 'UNCONFIRMED'),
    ])
    with app.state.db.connect() as con:
        before = {k: fingerprint(con, cid, k) for k in KINDS}
    request(client, 'PUT', f'/cases/{cid}/documents/{did}/statement-scope', {'paymentItems': True})
    with app.state.db.connect(write=True) as con:
        assert before['payslipValidation'] != fingerprint(con, cid, 'payslipValidation')
        for k in ('calculation', 'comparison', 'depositComparison'):
            assert before[k] == fingerprint(con, cid, k)
        con.execute('UPDATE fields SET value_json=? WHERE id=?', (dump(1100), ids['earning_1_amount']))
        assert before['depositComparison'] == fingerprint(con, cid, 'depositComparison')
