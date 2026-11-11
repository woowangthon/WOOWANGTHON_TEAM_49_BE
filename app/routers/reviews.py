"""Next.js review API; authentication is the same as the existing backend."""
from app.schemas import Envelope

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.routing import APIRoute
from pydantic import Field, StrictBool

from agent.review.registry import profiles
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import WorkRecord
from agent.schemas.public.wages import WageConditions
from app.db import load, now
from app.errors import ApiError, ok
from app.review_bridge import complete_review
from app.schemas import Input
from app.security import current_user

class ReviewRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except (ValueError, KeyError) as exc:
                raise ApiError(422, 'INVALID_REVIEW_INPUT', '검토 입력값과 원문 확인 상태를 확인해 주세요.') from exc
        return validated


router = APIRouter(tags=['Review workspaces'], route_class=ReviewRoute)
Revision = Annotated[int, Field(strict=True, ge=1)]


class ReviewCreate(Input):
    title: str = Field(default='새 월급 검토', min_length=1, max_length=200)
    auto_process: StrictBool = False
    auto_verify: StrictBool = False


class Basic(Input):
    employer_name: str | None = Field(default=None, max_length=200)
    pay_type: Literal['HOURLY', 'MONTHLY'] | None = None
    period_start: date | None = None
    period_end: date | None = None
    payment_date: date | None = None


class Options(Input):
    subjects_confirmed: StrictBool | None = None
    earnings_complete: StrictBool | None = None
    deductions_complete: StrictBool | None = None
    payment_scope_confirmed: StrictBool | None = None
    worked_time_kind: Literal['actual', 'paid'] | None = None
    slip_time_kind: Literal['actual', 'paid'] | None = None
    contract_part_id: str | None = Field(default=None, max_length=100)
    payslip_part_id: str | None = Field(default=None, max_length=100)


class ReviewUpdate(Input):
    revision: Revision
    title: str | None = Field(default=None, min_length=1, max_length=200)
    basic: Basic | None = None
    options: Options | None = None
    confirm_basic: StrictBool = False
    auto_process: StrictBool | None = None
    auto_verify: StrictBool | None = None


class Part(Input):
    document_type: Literal['contract', 'payslip', 'attendance', 'deposit']
    start_page: Annotated[int, Field(strict=True, ge=1)]
    end_page: Annotated[int, Field(strict=True, ge=1)]
    label: str = Field(default='', max_length=200)


class DocumentAction(Input):
    revision: Revision
    action: Literal['process', 'classify', 'manual', 'verify', 'exclude', 'include']
    parts: list[Part] = Field(default_factory=list, max_length=20)
    field_ids: list[str] = Field(default_factory=list, max_length=50)


class FieldEdit(Input):
    revision: Revision
    field_id: str = Field(min_length=1, max_length=100)
    value: str | None = Field(default=None, max_length=4000)
    unit: str | None = Field(default=None, max_length=30)
    candidate_id: str | None = Field(default=None, max_length=100)
    defer: StrictBool = False


class FieldAdd(Input):
    revision: Revision
    part_id: str = Field(min_length=1, max_length=100)
    row_type: Literal['earning', 'deduction', 'work', 'transaction'] | None = None
    label: str = Field(default='추가 정보', min_length=1, max_length=120)


class Confirm(Input):
    revision: Revision
    field_ids: list[str] = Field(min_length=1, max_length=2000)
    source_seen: StrictBool
    conflict_acknowledgement: str | None = Field(default=None, max_length=100)


class JobAction(Input):
    revision: Revision
    action: Literal['cancel', 'resume']


class CalculationInput(Input):
    wage_conditions: WageConditions | None = None
    work_records: list[WorkRecord] = Field(default_factory=list, max_length=1000)
    coverage: list[Period] = Field(default_factory=list, max_length=100)
    context_requirements: list[Period] = Field(default_factory=list, max_length=100)


class Complete(Input):
    revision: Revision
    workplace_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    case_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    analyze: StrictBool = True
    calculation_input: CalculationInput = Field(default_factory=CalculationInput)
    selected_transaction_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(default_factory=list, max_length=1000)


def values(body, *exclude):
    return body.model_dump(mode='json', exclude_unset=True, exclude=set(exclude), exclude_none=True)


@router.post('/reviews', status_code=201, response_model=Envelope)
def create(body: ReviewCreate, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.create(user['id'], **body.model_dump()), 201)


@router.get('/reviews', response_model=Envelope)
def list_reviews(request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    store = request.app.state.reviews.store
    with store.db.connect() as con:
        rows = con.execute('''SELECT r.* FROM review_sessions r WHERE owner_id=? AND deleted_at IS NULL
            AND (case_id IS NULL OR EXISTS(SELECT 1 FROM cases c JOIN workplaces w ON w.id=c.workplace_id
                WHERE c.id=r.case_id AND c.deleted_at IS NULL AND w.deleted_at IS NULL AND w.user_id=?))
            ORDER BY updated_at DESC,id LIMIT ? OFFSET ?''', (user['id'], user['id'], size, (page - 1) * size)).fetchall()
        items = [{'id': r['id'], 'revision': r['revision'], 'title': load(r['state_json'])['title'],
            'updated_at': r['updated_at']} for r in rows]
    return ok({'items': items, 'page': page, 'size': size, 'profiles': profiles()})


@router.get('/reviews/{rid}', response_model=Envelope)
def get_review(rid: str, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.store.view(user['id'], rid))


@router.put('/reviews/{rid}', response_model=Envelope)
def update(rid: str, body: ReviewUpdate, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.update(user['id'], rid, body.revision, values(body, 'revision')))


@router.delete('/reviews/{rid}', status_code=204)
def delete(rid: str, request: Request, revision: int = Query(ge=1), user=Depends(current_user)):
    store = request.app.state.reviews.store
    with store.db.connect(write=True) as con:
        row = store.owned(con, user['id'], rid)
        store.version(row, revision)
        con.execute('UPDATE review_sessions SET deleted_at=? WHERE id=?', (now(), rid))
        con.execute("UPDATE review_jobs SET status='CANCELLED',cancel_requested=1,execution_hash=NULL WHERE review_id=? AND status IN ('RUNNING','PENDING')", (rid,))
    return Response(status_code=204)


@router.post('/reviews/{rid}/documents', status_code=202, response_model=Envelope)
async def upload(rid: str, request: Request, revision: Annotated[int, Form(ge=1)],
                 files: Annotated[list[UploadFile], File()], user=Depends(current_user)):
    if not 1 <= len(files) <= 20:
        raise ApiError(422, 'REVIEW_FILE_COUNT_LIMIT')
    content = []
    total = 0
    for file in files:
        data = await file.read(request.app.state.settings.upload_max_bytes + 1)
        total += len(data)
        if len(data) > request.app.state.settings.upload_max_bytes or total > 80 * 1024 * 1024:
            raise ApiError(413, 'UPLOAD_TOO_LARGE')
        content.append((file.filename or 'document', data))
    return ok(await request.app.state.reviews.upload(user['id'], rid, revision, content), 202)


@router.post('/reviews/{rid}/documents/{did}', response_model=Envelope)
def document_action(rid: str, did: str, body: DocumentAction, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.document_action(user['id'], rid, did, body.revision,
        body.action, body.model_dump(mode='json', exclude={'revision', 'action'})))


@router.get('/reviews/{rid}/documents/{did}/original', response_model=Envelope)
def original(rid: str, did: str, request: Request, user=Depends(current_user)):
    document, data = request.app.state.reviews.source(user['id'], rid, did)
    return Response(data, media_type=document['media_type'], headers={'Content-Disposition': 'inline'})


@router.get('/reviews/{rid}/documents/{did}/pages/{page}.png', response_model=Envelope)
async def page_image(rid: str, did: str, page: int, request: Request, user=Depends(current_user)):
    return Response(await request.app.state.reviews.page(user['id'], rid, did, page), media_type='image/png')


@router.put('/reviews/{rid}/fields', response_model=Envelope)
def edit(rid: str, body: FieldEdit, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.edit(user['id'], rid, body.revision, values(body, 'revision')))


@router.post('/reviews/{rid}/fields', response_model=Envelope)
def add(rid: str, body: FieldAdd, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.add_fields(user['id'], rid, body.revision,
        body.part_id, body.row_type, body.label))


@router.post('/reviews/{rid}/confirm', response_model=Envelope)
def confirm(rid: str, body: Confirm, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.confirm(user['id'], rid, body.revision,
        body.field_ids, body.source_seen, body.conflict_acknowledgement))


@router.get('/reviews/{rid}/jobs/{jid}', response_model=Envelope)
def job(rid: str, jid: str, request: Request, user=Depends(current_user)):
    store = request.app.state.reviews.store
    with store.db.connect() as con:
        store.owned(con, user['id'], rid)
        row = con.execute('SELECT * FROM review_jobs WHERE id=? AND review_id=?', (jid, rid)).fetchone()
        if row is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        return ok(store.job_view(row))


@router.post('/reviews/{rid}/jobs/{jid}', response_model=Envelope)
def job_action(rid: str, jid: str, body: JobAction, request: Request, user=Depends(current_user)):
    return ok(request.app.state.reviews.control_job(user['id'], rid, jid, body.action, body.revision))


@router.get('/reviews/{rid}/history', response_model=Envelope)
def history(rid: str, request: Request, page: int = Query(1, ge=1), user=Depends(current_user)):
    store = request.app.state.reviews.store
    with store.db.connect() as con:
        store.owned(con, user['id'], rid)
        rows = con.execute('SELECT * FROM review_events WHERE review_id=? ORDER BY created_at,id LIMIT 50 OFFSET ?',
            (rid, (page - 1) * 50)).fetchall()
        return ok({'items': [dict(row) | {'data': load(row['data_json'])} for row in rows], 'page': page})


@router.post('/reviews/{rid}/complete', status_code=202, response_model=Envelope)
async def complete(rid: str, body: Complete, request: Request, user=Depends(current_user)):
    return ok(await complete_review(request.app.state.reviews, user['id'], rid, body.revision,
        body.model_dump(mode='json', exclude={'revision'})), 202)
