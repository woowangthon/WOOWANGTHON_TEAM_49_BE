from fastapi import APIRouter, Depends, Query, Request, Response

from app.db import dump, load, now
from app.engine import ALL_CHECKS, latest, results, schedule, snapshot_accessible, snapshot_dto
from app.errors import ApiError, ok
from app.idempotency import idempotency_header
from app.routers.workspaces import page_data
from app.schemas import ConditionsPut, Execute, Inquiry, ManualCreate
from app.security import case, check_version, current_user

router = APIRouter(tags=['Analysis & Results'])
CONDITIONS = [
    {'conditionId': 'workplace_size', 'question': '상시 근로자 수를 알고 있나요?', 'type': 'SELECT',
     'options': ['LESS_THAN_5', 'AT_LEAST_5', 'UNKNOWN'], 'requiredFor': ['OVERTIME_PAY']},
    {'conditionId': 'regular_minutes', 'question': '이번 기간의 확인된 기본급 산정시간은 몇 분인가요?',
     'type': 'NUMBER', 'unit': 'MINUTE', 'options': None, 'requiredFor': ['WAGE_CALCULATION']},
    {'conditionId': 'base_scope_confirmed', 'question': '기본급의 산정기간·임금 구성·시간 기준이 이 사례와 일치하나요?',
     'type': 'BOOLEAN', 'options': None, 'requiredFor': ['WAGE_CALCULATION']},
    {'conditionId': 'comparison_basis_confirmed', 'question': '계약과 명세서의 기간·시급 기준이 같은지 확인했나요?',
     'type': 'BOOLEAN', 'options': None, 'requiredFor': ['CONTRACT_ATTENDANCE_COMPARISON']},
    {'conditionId': 'hours_basis_confirmed', 'question': '근태와 명세서의 산정기간 및 실근로시간 기준이 같나요? 유급시간과 구분해 주세요.',
     'type': 'BOOLEAN', 'options': None, 'requiredFor': ['CONTRACT_ATTENDANCE_COMPARISON']},
    {'conditionId': 'base_pay_basis_confirmed', 'question': '기본급과 명세서 기본급의 기간·포함 항목이 같은지 확인했나요?',
     'type': 'BOOLEAN', 'options': None, 'requiredFor': ['CONTRACT_ATTENDANCE_COMPARISON']},
]


def condition_dto(con, case_id):
    row = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    answers = load(row['conditions'])
    return {'conditions': [{**c, 'value': answers.get(c['conditionId']),
        'status': 'REQUIRED' if c['conditionId'] not in answers else ('UNKNOWN' if answers[c['conditionId']] == 'UNKNOWN' else 'ANSWERED'),
        'source': 'USER_INPUT' if c['conditionId'] in answers else None} for c in CONDITIONS], 'inputVersion': row['input_version']}


def readiness_data(con, case_id):
    group = con.execute('SELECT * FROM comparison_groups WHERE id=?', (f'default-{case_id}',)).fetchone()
    return {'caseId': case_id, 'inputVersion': con.execute('SELECT input_version FROM cases WHERE id=?',(case_id,)).fetchone()[0],
        'checks': [{'checkType': check, 'available': bool(group and group['confirmed_json'] != '{}' and not group['needs_review']),
        'mode': 'AGENT_SUPPORTED_SCOPE', 'missingInputs': [], 'reasonCode': 'USE_COMPARISON_GROUP_CONFIRMED_INPUT'} for check in ALL_CHECKS]}

@router.get('/cases/{case_id}/conditions')
def get_conditions(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(condition_dto(con, case_id))


@router.put('/cases/{case_id}/conditions')
def put_conditions(case_id: int, body: ConditionsPut, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        answers = load(c['conditions'])
        seen = set()
        for answer in body.answers:
            key, value = answer.conditionId, answer.value
            if key in seen or key not in {v['conditionId'] for v in CONDITIONS}:
                raise ApiError(422, 'INVALID_CONDITION_ID')
            seen.add(key)
            if value != 'UNKNOWN':
                valid = ((key == 'regular_minutes' and type(value) is int and 0 <= value <= 1000000) or
                         (key in ('base_scope_confirmed', 'comparison_basis_confirmed', 'hours_basis_confirmed', 'base_pay_basis_confirmed') and type(value) is bool) or
                         (key == 'workplace_size' and value in ('LESS_THAN_5', 'AT_LEAST_5')))
                if not valid:
                    raise ApiError(422, 'INVALID_CONDITION_VALUE', details={'conditionId': key})
            answers[key] = value
        con.execute('UPDATE cases SET conditions=? WHERE id=?', (dump(answers), case_id))
        mutation = schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)
        return ok({'remainingQuestions': sum(c['conditionId'] not in answers or answers[c['conditionId']] == 'UNKNOWN' for c in CONDITIONS),
                   'calculationAvailable': readiness_data(con, case_id)['checks'][0]['available'], **mutation})


@router.get('/cases/{case_id}/readiness')
def readiness(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(readiness_data(con, case_id))


def manual_dto(row):
    return {'recordId': row['id'], 'caseId': row['case_id'], 'recordType': row['record_type'],
            'data': load(row['data_json']), 'source': 'USER_INPUT', 'createdAt': row['created_at']}


def validate_manual_period(c, body):
    if body.recordType == 'ATTENDANCE' and not c['period_start'] <= body.data['workDate'] <= c['period_end']:
        raise ApiError(422, 'INVALID_PERIOD', '근무일이 사례 산정기간 밖입니다.')


@router.post('/cases/{case_id}/manual-records', status_code=201)
def create_manual(case_id: int, body: ManualCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        validate_manual_period(c, body)
        if body.recordType == 'DEPOSIT':
            from app.routers.payments import insert_transaction, insert_allocation
            tid = insert_transaction(con, c['workplace_id'], body.data)
            allocation = insert_allocation(con, case_id, tid, body.data['amount'])
            return ok({'recordId': None, 'transactionId': tid, 'allocationId': allocation['allocationId'],
                'canonicalResource': 'PAYMENT_TRANSACTION',
                **schedule(con, case_id, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds)}, 201)
        rid = con.execute('INSERT INTO manual_records(case_id,record_type,data_json,created_at) VALUES(?,?,?,?)',
                          (case_id, body.recordType, dump(body.data), now())).lastrowid
        return ok({'recordId': rid, **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)}, 201)


@router.get('/cases/{case_id}/manual-records')
def list_manual(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(page_data([manual_dto(row) for row in con.execute('SELECT * FROM manual_records WHERE case_id=? ORDER BY id', (case_id,))], page, size))


def manual_record(con, case_id, record_id):
    row = con.execute('SELECT * FROM manual_records WHERE id=? AND case_id=?', (record_id, case_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


@router.get('/cases/{case_id}/manual-records/{record_id}')
def get_manual(case_id: int, record_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(manual_dto(manual_record(con, case_id, record_id)))


@router.patch('/cases/{case_id}/manual-records/{record_id}')
def patch_manual(case_id: int, record_id: int, body: ManualCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        original = manual_record(con, case_id, record_id)
        if original['record_type'] != body.recordType:
            raise ApiError(422, 'RECORD_TYPE_IMMUTABLE')
        validate_manual_period(c, body)
        con.execute('UPDATE manual_records SET data_json=? WHERE id=?', (dump(body.data), record_id))
        return ok({'recordId': record_id, **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)})


@router.delete('/cases/{case_id}/manual-records/{record_id}', status_code=204)
def delete_manual(case_id: int, record_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        manual_record(con, case_id, record_id)
        con.execute('DELETE FROM manual_records WHERE id=?', (record_id,))
        schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


@router.get('/cases/{case_id}/results')
def get_results(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(results(con, case_id))


@router.get('/cases/{case_id}/recompute-status')
def recompute_status(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        c = case(con, case_id, user['id'])
        row = con.execute('SELECT id FROM agent_jobs WHERE case_id=? ORDER BY created_at DESC LIMIT 1',(case_id,)).fetchone()
    return ok({'caseId': case_id, 'currentInputVersion': c['input_version'],
        'latestJob': request.app.state.service.store.job(user['id'],row['id']) if row else None})


@router.get('/cases/{case_id}/recompute-jobs/{job_id}')
def get_job(case_id: int, job_id: str, request: Request, user=Depends(current_user)):
    job=request.app.state.service.store.job(user['id'],job_id)
    if job['case_id'] != case_id:
        raise ApiError(404,'RESOURCE_NOT_FOUND')
    return ok(job)


def queue_checks(case_id, body, request, user, checks):
    with request.app.state.db.connect() as con:
        check_version(case(con,case_id,user['id']),body.expectedInputVersion)
    if not body.confirmed:
        raise ApiError(422,'CONFIRMATION_REQUIRED')
    jid=request.app.state.service.enqueue_analysis(user['id'],f'default-{case_id}',idempotency_key=request.headers.get('Idempotency-Key'))
    return ok({'jobId':jid,'status':'PENDING','checks':checks},202)

@router.post('/cases/{case_id}/recompute', status_code=202, dependencies=[Depends(idempotency_header)])
def recompute(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ALL_CHECKS)


@router.post('/cases/{case_id}/calculations', status_code=202, dependencies=[Depends(idempotency_header)])
def calculations(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ['WAGE_CALCULATION'])


@router.post('/cases/{case_id}/comparisons', status_code=202, dependencies=[Depends(idempotency_header)])
def comparisons(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ['CONTRACT_ATTENDANCE_COMPARISON'])


@router.post('/cases/{case_id}/payslip-validations', status_code=202, dependencies=[Depends(idempotency_header)])
def payslip_validations(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ['PAYSLIP_VALIDATION'])


def snapshot_list(con, case_id, kind, page, size):
    _, ref = latest(con, case_id, kind)
    rows = con.execute('SELECT * FROM agent_results WHERE case_id=? AND group_id=? ORDER BY version DESC', (case_id, f'default-{case_id}')).fetchall()
    return page_data([snapshot_dto(row, ref['freshness'] if row['id'] == ref['snapshotId'] else 'STALE')
        for row in rows if snapshot_accessible(con, row)], page, size)


@router.get('/cases/{case_id}/calculations')
def list_calculations(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(snapshot_list(con, case_id, 'calculation', page, size))


@router.get('/cases/{case_id}/calculations/{calculation_id}')
def get_calculation(case_id: int, calculation_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        row = con.execute("SELECT * FROM agent_results WHERE id=? AND case_id=? AND group_id=?", (calculation_id, case_id, f'default-{case_id}')).fetchone()
        if row is None or not snapshot_accessible(con, row):
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        current, ref = latest(con, case_id, 'calculation')
        return ok(snapshot_dto(row, ref['freshness'] if current and row['id'] == current['id'] else 'STALE'))


@router.get('/cases/{case_id}/comparisons')
def list_comparisons(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(snapshot_list(con, case_id, 'comparison', page, size))


@router.get('/cases/{case_id}/payslip-validations')
def list_validations(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(snapshot_list(con, case_id, 'payslipValidation', page, size))


@router.get('/cases/{case_id}/questions')
def questions(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con,case_id,user['id'])
        row,ref=latest(con,case_id)
        return ok({'questions':load(row['data_json'])['questions'] if row else [],'resultId':ref['snapshotId']})


@router.post('/cases/{case_id}/company-inquiry')
def inquiry(case_id: int, body: Inquiry, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con,case_id,user['id'])
        row,ref=latest(con,case_id)
        if row is None:
            raise ApiError(409,'RESULT_REQUIRED')
    return ok(request.app.state.service.inquiry(user['id'],row['id']))
