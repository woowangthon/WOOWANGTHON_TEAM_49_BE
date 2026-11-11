from fastapi import APIRouter, Depends, Query, Request, Response

from app.db import now
from app.engine import results, schedule
from app.errors import ApiError, ok
from app.outputs import API, CaseDTO, Page, WorkplaceDTO
from app.schemas import CaseCreate, CasePatch, WorkplaceCreate, WorkplacePatch, Workflow
from app.security import case, check_version, current_user, workplace

router = APIRouter(tags=['Workplaces & Cases'])


def page_data(items, page=1, size=20):
    return {'items': items[(page-1)*size:page*size], 'page': page, 'size': size, 'total': len(items)}


def workplace_dto(con, row):
    return {'workplaceId': row['id'], 'name': row['name'], 'industry': row['industry'], 'region': row['region'],
        'employmentStatus': row['employment_status'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
        'caseCount': con.execute('SELECT COUNT(*) FROM cases WHERE workplace_id=? AND deleted_at IS NULL', (row['id'],)).fetchone()[0]}


def case_dto(con, row):
    w = con.execute('SELECT * FROM workplaces WHERE id=?', (row['workplace_id'],)).fetchone()
    result = results(con, row['id'])
    attention = sum(item['status'] != 'match' for item in result['items'])
    attention += sum(item['status'] in ('insufficient', 'needs_review')
        for item in (result['calculation'] or {}).get('items', []))
    return {'caseId': row['id'], 'workplaceId': row['workplace_id'],
        'workplace': {'workplaceId': w['id'], 'name': w['name'], 'industry': w['industry'], 'region': w['region']},
        'payType': row['pay_type'], 'periodStart': row['period_start'], 'periodEnd': row['period_end'],
        'scheduledPayDate': row['scheduled_pay_date'], 'status': row['status'], 'currentStep': row['current_step'],
        'inputVersion': row['input_version'], 'updatedAt': row['updated_at'],
        'documentCount': con.execute('SELECT COUNT(*) FROM document_links l JOIN documents d ON d.id=l.document_id WHERE l.case_id=? AND d.deleted_at IS NULL', (row['id'],)).fetchone()[0],
        'latestCalculationId': result['versions']['calculation']['snapshotId'],
        'attentionRequired': bool(attention or result['freshness']['hasOutdatedResults']), 'attentionCount': attention}


@router.post('/workplaces', status_code=201)
def create_workplace(body: WorkplaceCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        timestamp = now()
        wid = con.execute('INSERT INTO workplaces(user_id,name,industry,region,employment_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            (user['id'], body.name, body.industry, body.region, body.employmentStatus, timestamp, timestamp)).lastrowid
    return ok({'workplaceId': wid}, 201)


@router.get('/workplaces', response_model=API[Page[WorkplaceDTO]])
def list_workplaces(request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        items = [workplace_dto(con, row) for row in con.execute('SELECT * FROM workplaces WHERE user_id=? AND deleted_at IS NULL ORDER BY id DESC', (user['id'],))]
    return ok(page_data(items, page, size))


@router.get('/workplaces/{workplace_id}', response_model=API[WorkplaceDTO])
def get_workplace(workplace_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(workplace_dto(con, workplace(con, workplace_id, user['id'])))


@router.patch('/workplaces/{workplace_id}')
def patch_workplace(workplace_id: int, body: WorkplacePatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        fields = {'name': 'name', 'industry': 'industry', 'region': 'region', 'employmentStatus': 'employment_status'}
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is None and key in ('name', 'employmentStatus'):
                raise ApiError(422, 'INVALID_INPUT')
            con.execute(f'UPDATE workplaces SET {fields[key]}=?,updated_at=? WHERE id=?', (value, now(), workplace_id))
        return ok(workplace_dto(con, workplace(con, workplace_id, user['id'])))


@router.delete('/workplaces/{workplace_id}', status_code=204)
def delete_workplace(workplace_id: int, request: Request, confirm: bool = False, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        count = con.execute('SELECT COUNT(*) FROM cases WHERE workplace_id=? AND deleted_at IS NULL', (workplace_id,)).fetchone()[0]
        if count and not confirm:
            raise ApiError(409, 'WORKPLACE_REFERENCES_EXIST', details={'caseCount': count, 'confirmationQuery': 'confirm=true'})
        con.execute('UPDATE workplaces SET deleted_at=? WHERE id=?', (now(), workplace_id))
    return Response(status_code=204)


@router.post('/cases', status_code=201)
def create_case(body: CaseCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, body.workplaceId, user['id'])
        cid = con.execute('INSERT INTO cases(workplace_id,pay_type,period_start,period_end,scheduled_pay_date,updated_at) VALUES(?,?,?,?,?,?)',
            (body.workplaceId, body.payType, str(body.periodStart), str(body.periodEnd),
             str(body.scheduledPayDate) if body.scheduledPayDate else None, now())).lastrowid
        scheduled = schedule(con, cid, increment=False)
    return ok({'caseId': cid, 'status': 'CREATED', **scheduled}, 201)


@router.get('/cases', response_model=API[Page[CaseDTO]])
def list_cases(request: Request, workplaceId: int | None = None, page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        if workplaceId is not None:
            workplace(con, workplaceId, user['id'])
        rows = con.execute('''SELECT c.* FROM cases c JOIN workplaces w ON w.id=c.workplace_id
            WHERE w.user_id=? AND c.deleted_at IS NULL AND w.deleted_at IS NULL
            AND (? IS NULL OR c.workplace_id=?) ORDER BY c.id DESC''', (user['id'], workplaceId, workplaceId)).fetchall()
        return ok(page_data([case_dto(con, row) for row in rows], page, size))


@router.get('/workplaces/{workplace_id}/cases', response_model=API[Page[CaseDTO]])
def workplace_cases(workplace_id: int, request: Request, page: int = Query(1, ge=1),
                    size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    return list_cases(request, workplace_id, page, size, user)


@router.get('/cases/{case_id}', response_model=API[CaseDTO])
def get_case(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(case_dto(con, case(con, case_id, user['id'])))


@router.patch('/cases/{case_id}')
def patch_case(case_id: int, body: CasePatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        row = case(con, case_id, user['id'])
        check_version(row, body.expectedInputVersion)
        updates = body.model_dump(mode='json', exclude_unset=True, exclude={'expectedInputVersion'})
        fields = {'payType': 'pay_type', 'periodStart': 'period_start', 'periodEnd': 'period_end', 'scheduledPayDate': 'scheduled_pay_date'}
        if any(v is None for k, v in updates.items() if k != 'scheduledPayDate'):
            raise ApiError(422, 'INVALID_INPUT')
        if updates.get('periodStart', row['period_start']) > updates.get('periodEnd', row['period_end']):
            raise ApiError(422, 'INVALID_PERIOD')
        for key, value in updates.items():
            con.execute(f'UPDATE cases SET {fields[key]}=? WHERE id=?', (value, case_id))
        return ok({'caseId': case_id, **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)})


@router.patch('/cases/{case_id}/workflow')
def workflow(case_id: int, body: Workflow, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is None:
                raise ApiError(422, 'INVALID_INPUT')
            con.execute(f'UPDATE cases SET {"current_step" if key == "currentStep" else "status"}=?,updated_at=? WHERE id=?', (value, now(), case_id))
        return ok(case_dto(con, case(con, case_id, user['id'])))


@router.get('/cases/{case_id}/deletion-impact')
def deletion_impact(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        docs = con.execute('SELECT document_id FROM document_links WHERE case_id=?', (case_id,)).fetchall()
        shared = sum(con.execute('SELECT COUNT(*) FROM document_links WHERE document_id=? AND case_id!=?', (d[0], case_id)).fetchone()[0] > 0 for d in docs)
        return ok({'caseId': case_id, 'linkedDocumentCount': len(docs), 'sharedDocumentCount': shared,
                   'reportCount': con.execute('SELECT COUNT(*) FROM reports WHERE case_id=?', (case_id,)).fetchone()[0],
                   'willUnlinkSharedDocuments': True, 'willDeleteSharedOriginals': False, 'willRevokeCaseAccessImmediately': True})


@router.delete('/cases/{case_id}', status_code=204)
def delete_case(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        con.execute('UPDATE cases SET deleted_at=? WHERE id=?', (now(), case_id))
        con.execute('DELETE FROM document_links WHERE case_id=?', (case_id,))
        con.execute('DELETE FROM allocations WHERE case_id=?', (case_id,))
        con.execute("UPDATE jobs SET status='SUPERSEDED',completed_at=? WHERE case_id=? AND status IN ('PENDING','RUNNING')", (now(), case_id))
    return Response(status_code=204)
