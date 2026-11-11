"""Backend compatibility views; all arithmetic runs exclusively in agent.rules."""
from app.db import load, now
from app.integration_store import ensure_default_group, check_historical_access
from agent.schemas.public.inputs import SourceRef
from app.agent_mapping import result_response
from agent.schemas.public.results import AnalysisResult

KINDS = {'calculation': 'WAGE_CALCULATION', 'comparison': 'CONTRACT_ATTENDANCE_COMPARISON',
         'payslipValidation': 'PAYSLIP_VALIDATION', 'depositComparison': 'DEPOSIT_COMPARISON'}
ALL_CHECKS = list(KINDS.values())


def schedule(con, case_id, checks=ALL_CHECKS, delay=0.3, increment=True):
    """Invalidate saved inputs. Explicit confirmed-group execution creates a job."""
    ensure_default_group(con, case_id)
    if increment:
        con.execute('UPDATE cases SET input_version=input_version+1,updated_at=? WHERE id=?', (now(), case_id))
        con.execute('UPDATE comparison_groups SET version=version+1,needs_review=1,updated_at=? WHERE case_id=?', (now(), case_id))
    version = con.execute('SELECT input_version FROM cases WHERE id=?', (case_id,)).fetchone()[0]
    return {'inputVersion': version, 'recompute': {'scheduled': False, 'jobId': None,
        'affectedChecks': list(checks), 'action': 'CONFIRM_COMPARISON_GROUP'}}


def snapshot_accessible(con, row):
    try:
        snapshot = con.execute('SELECT data_json FROM agent_snapshots WHERE id=?', (row['snapshot_id'],)).fetchone()
        check_historical_access(con, row['owner_id'], row['case_id'], row['group_id'],
            [SourceRef.model_validate(s) for s in load(snapshot[0])['authorized_sources']])
        return True
    except Exception:
        return False


def latest(con, case_id, kind=None, data=None):
    group = con.execute('SELECT * FROM comparison_groups WHERE id=? AND deleted_at IS NULL', (f'default-{case_id}',)).fetchone()
    row = con.execute('SELECT * FROM agent_results WHERE id=?', (group['latest_result_id'],)).fetchone() if group and group['latest_result_id'] else None
    if row is not None and not snapshot_accessible(con, row):
        row = None
    case = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    snap = con.execute('SELECT * FROM agent_snapshots WHERE id=?', (row['snapshot_id'],)).fetchone() if row else None
    current = bool(row and not group['needs_review'] and snap['input_version']==case['input_version'] and snap['group_version']==group['version'])
    return row, {'snapshotId': row['id'] if row else None, 'version': row['version'] if row else None,
        'inputVersion': snap['input_version'] if snap else case['input_version'], 'createdAt': row['created_at'] if row else None,
        'freshness': 'CURRENT' if current else 'STALE'}


def snapshot_dto(row, freshness='CURRENT'):
    result = AnalysisResult.model_validate_json(row['data_json'])
    result.freshness = freshness.lower()
    return result_response(result)


def results(con, case_id):
    row, ref = latest(con, case_id)
    value = snapshot_dto(row, ref['freshness']) if row else None
    raw = load(row['data_json']) if row else None
    comparison_states = {item['status'] for item in raw['comparisons']} if raw else {'insufficient'}
    comparison_status = next((state for state in ('difference', 'needs_review', 'insufficient') if state in comparison_states), 'match')
    return {'caseId': case_id, 'comparisonGroupId': f'default-{case_id}',
        'currentInputVersion': con.execute('SELECT input_version FROM cases WHERE id=?', (case_id,)).fetchone()[0],
        'resultVersion': row['version'] if row else 0, 'result': value,
        'summary': {'calculationStatus': ('COMPLETE' if raw['wage_section']['scope_complete'] else 'PARTIAL') if raw else 'INSUFFICIENT_INFORMATION',
                    'comparisonStatus': {'match':'MATCH','difference':'DIFFERENCE','needs_review':'REVIEW_REQUIRED','insufficient':'INSUFFICIENT_INFORMATION'}[comparison_status]},
        'freshness': {'overall': ref['freshness'], 'hasOutdatedResults': ref['freshness']!='CURRENT'},
        'versions': {kind: ref for kind in KINDS}, 'items': raw['comparisons'] if raw else [],
        'calculation': raw['wage_section'] if raw else None, 'comparison': raw['comparisons'] if raw else None,
        'payslipValidation': None, 'depositComparison': None}
def effective_fields(con, case_id, document_id):
    result = []
    for field in con.execute('SELECT * FROM fields WHERE document_id=? ORDER BY id', (document_id,)):
        override = con.execute('SELECT * FROM field_overrides WHERE case_id=? AND field_id=?', (case_id, field['id'])).fetchone()
        active = override or field
        result.append({'fieldId': field['id'], 'key': field['key'], 'label': field['label'],
            'value': load(active['value_json']), 'valueType': field['value_type'], 'status': active['status'],
            'editedByUser': bool(override or field['edited']), 'confirmedAt': active['confirmed_at'],
            'effectiveValueOrigin': 'CASE_OVERRIDE' if override else 'SHARED_DEFAULT',
            'hasCaseOverride': override is not None,
            'source': load(override['source_json']) if override and override['source_json'] else load(field['source_json'])})
    return result
