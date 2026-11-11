"""Next.js-facing orchestration API; no frontend or development-store dependency."""
from app.schemas import Envelope
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field

from agent.schemas.public.tasks import SupplementTarget
from app.agent_mapping import group_materials, result_response, snapshot_from_group
from app.db import dump, load, now
from app.errors import ApiError, ok
from app.integration_store import owned_document, owned_group, source_ref
from app.schemas import Input
from app.security import case, current_user

router = APIRouter(tags=['Integrated Agent'])


class GroupCreate(Input):
    name: str = Field(default='비교 묶음', min_length=1, max_length=100)
    documentIds: list[int] = Field(default_factory=list, max_length=20)


class GroupDocuments(Input):
    expectedVersion: int = Field(ge=1)
    documentIds: list[int] = Field(max_length=20)


class ConfirmedInput(Input):
    expectedVersion: int = Field(ge=1)
    confirmedInput: dict


class ExtractionRequest(Input):
    documentType: Literal['auto', 'contract', 'attendance', 'payslip', 'deposit', 'payslip_ocr'] | None = None
    targetFields: list[SupplementTarget] = Field(default_factory=list, max_length=50)


class ReportRequest(Input):
    explanationJobId: str
    documentIds: list[int] = Field(default_factory=list, max_length=20)


class InquiryRequest(Input):
    questionIds: list[str] | None = Field(default=None, max_length=100)
    text: str | None = Field(default=None, min_length=1, max_length=20000)


class CandidateReview(Input):
    candidateId: str
    fieldId: str
    value: str | None = None
    manual: bool = False
    defer: bool = False
    unit: str | None = None


def version_check(group, expected):
    if group['version'] != expected:
        raise ApiError(409, 'GROUP_VERSION_CONFLICT', details={'currentVersion': group['version']})


def group_dto(con, row):
    return {'groupId': row['id'], 'caseId': row['case_id'], 'name': row['name'], 'version': row['version'],
        'needsReview': bool(row['needs_review']), 'latestResultId': row['latest_result_id'],
        'documentIds': [r[0] for r in con.execute('SELECT document_id FROM group_documents WHERE group_id=? ORDER BY document_id', (row['id'],))],
        'confirmedInput': load(row['confirmed_json']), 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}


def set_documents(con, group, owner_id, ids):
    if len(set(ids)) != len(ids):
        raise ApiError(422, 'DUPLICATE_DOCUMENT_SELECTION')
    refs = [source_ref(con, did, owner_id, group['case_id']) for did in ids]
    from app.review_store import validate_part_selection
    validate_part_selection(con, ids)
    kinds = []
    for ref in refs:
        reading = con.execute('''SELECT id FROM document_ocr WHERE document_id=? AND document_version=?
            AND content_hash=? ORDER BY created_at DESC,id DESC LIMIT 1''',
            (ref.document_id, ref.document_version, ref.content_hash)).fetchone()
        candidate = con.execute('''SELECT data_json FROM extraction_candidates WHERE document_id=?
            AND document_version=? AND ocr_result_id=? AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1''',
            (ref.document_id, ref.document_version, reading['id'] if reading else None)).fetchone()
        if candidate is None:
            raise ApiError(409, 'DOCUMENT_EXTRACTION_REQUIRED', details={'documentId': ref.document_id})
        kinds.append(load(candidate['data_json'])['document_type'])
    if kinds.count('contract') > 1 or sum(k in ('payslip', 'payslip_ocr') for k in kinds) > 1:
        raise ApiError(422, 'ONE_CONTRACT_AND_PAYSLIP_PER_GROUP')
    con.execute('DELETE FROM group_documents WHERE group_id=?', (group['id'],))
    con.executemany('INSERT INTO group_documents(group_id,document_id,document_version,link_version) VALUES(?,?,?,?)',
        [(group['id'], int(ref.document_id), ref.document_version, ref.link_version) for ref in refs])


@router.post('/cases/{case_id}/comparison-groups', status_code=201, response_model=Envelope)
def create_group(case_id: int, body: GroupCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        gid = uuid.uuid4().hex
        con.execute('INSERT INTO comparison_groups(id,case_id,name,created_at,updated_at) VALUES(?,?,?,?,?)',
            (gid, case_id, body.name, now(), now()))
        group = owned_group(con, gid, user['id'])
        set_documents(con, group, user['id'], body.documentIds)
        return ok(group_dto(con, group), 201)


@router.get('/cases/{case_id}/comparison-groups', response_model=Envelope)
def list_groups(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok({'items': [group_dto(con, row) for row in con.execute(
            "SELECT * FROM comparison_groups WHERE case_id=? AND deleted_at IS NULL AND id NOT LIKE 'compat-%' ORDER BY created_at", (case_id,))]})


@router.get('/comparison-groups/{group_id}', response_model=Envelope)
def get_group(group_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(group_dto(con, owned_group(con, group_id, user['id'])))


@router.put('/comparison-groups/{group_id}/documents', response_model=Envelope)
def select_documents(group_id: str, body: GroupDocuments, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        group = owned_group(con, group_id, user['id'])
        version_check(group, body.expectedVersion)
        set_documents(con, group, user['id'], body.documentIds)
        con.execute('UPDATE comparison_groups SET version=version+1,needs_review=1,updated_at=? WHERE id=?', (now(), group_id))
        return ok(group_dto(con, owned_group(con, group_id, user['id'])))


@router.get('/comparison-groups/{group_id}/materials', response_model=Envelope)
def materials(group_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(group_materials(con, group_id, user['id']))


@router.put('/comparison-groups/{group_id}/confirmed-input', response_model=Envelope)
def confirm_input(group_id: str, body: ConfirmedInput, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        group = owned_group(con, group_id, user['id'])
        version_check(group, body.expectedVersion)
        # Saving is explicit confirmation of currently selected material versions.
        ids = [r[0] for r in con.execute('SELECT document_id FROM group_documents WHERE group_id=?', (group_id,))]
        set_documents(con, group, user['id'], ids)
        con.execute('UPDATE comparison_groups SET confirmed_json=?,version=version+1,needs_review=0,updated_at=? WHERE id=?',
            (dump(body.confirmedInput), now(), group_id))
        try:
            snapshot_from_group(con, group_id, user['id'], 'validation-only')
        except ValueError as exc:
            raise ApiError(422, 'INVALID_CONFIRMED_INPUT', details={'reason': str(exc)}) from exc
        updated = owned_group(con, group_id, user['id'])
        con.execute('INSERT INTO group_confirmations(id,group_id,version,data_json,created_at) VALUES(?,?,?,?,?)',
            (uuid.uuid4().hex, group_id, updated['version'], dump(body.confirmedInput), now()))
        return ok(group_dto(con, updated))


@router.delete('/comparison-groups/{group_id}', status_code=204)
def delete_group(group_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        group = owned_group(con, group_id, user['id'])
        if group_id == f'default-{group["case_id"]}':
            raise ApiError(409, 'DEFAULT_GROUP_REQUIRED')
        con.execute('UPDATE comparison_groups SET deleted_at=? WHERE id=?', (now(), group_id))
        con.execute("UPDATE agent_jobs SET status='CANCELLED',cancel_requested=1,execution_hash=NULL WHERE group_id=? AND status IN ('PENDING','RUNNING')", (group_id,))
    return Response(status_code=204)


def queued(job_id):
    return ok({'jobId': job_id, 'status': 'PENDING', 'statusUrl': f'/api/v1/agent-jobs/{job_id}'}, 202)


@router.post('/comparison-groups/{group_id}/analyze', status_code=202, response_model=Envelope)
def analyze(group_id: str, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.enqueue_analysis(user['id'], group_id, idempotency_key=request.headers.get('Idempotency-Key')))


@router.get('/comparison-groups/{group_id}/results', response_model=Envelope)
def result_history(group_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        group = owned_group(con, group_id, user['id'])
        items = []
        for row in con.execute('SELECT id FROM agent_results WHERE group_id=? ORDER BY version DESC', (group_id,)):
            try:
                result, _, _ = request.app.state.service.store.result(con, user['id'], row['id'])
                items.append(result_response(result))
            except Exception as exc:
                from agent.schemas.public.errors import AccessRevoked
                if isinstance(exc, AccessRevoked):
                    continue
                raise
        return ok({'groupId': group_id, 'latestResultId': group['latest_result_id'], 'items': items})


@router.get('/agent-results/{result_id}', response_model=Envelope)
def get_result(result_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        result, row, snapshot = request.app.state.service.store.result(con, user['id'], result_id)
        return ok({'groupId': row['group_id'], 'result': result_response(result), 'snapshot': snapshot})


@router.post('/documents/{document_id}/read', status_code=202, response_model=Envelope)
def read_document(document_id: int, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.enqueue_read(user['id'], document_id, idempotency_key=request.headers.get('Idempotency-Key')))


@router.post('/documents/{document_id}/extract', status_code=202, response_model=Envelope)
def extract(document_id: int, request: Request, body: ExtractionRequest = ExtractionRequest(), user=Depends(current_user)):
    if body.targetFields:
        raise ApiError(422, 'USE_SUPPLEMENT_ENDPOINT')
    return queued(request.app.state.service.enqueue_extraction(user['id'], document_id,
        document_type=body.documentType, idempotency_key=request.headers.get('Idempotency-Key')))


@router.post('/documents/{document_id}/supplement', status_code=202, response_model=Envelope)
def supplement(document_id: int, body: ExtractionRequest, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.enqueue_extraction(user['id'], document_id,
        method='pdf_supplement', target_fields=body.targetFields, idempotency_key=request.headers.get('Idempotency-Key')))


@router.get('/documents/{document_id}/processing-history', response_model=Envelope)
def processing_history(document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc = owned_document(con, document_id, user['id'])
        readings = [dict(row) for row in con.execute('SELECT * FROM document_ocr WHERE document_id=? ORDER BY created_at', (document_id,))]
        candidates = [dict(row) for row in con.execute('SELECT * FROM extraction_candidates WHERE document_id=? ORDER BY created_at', (document_id,))]
        for row in readings + candidates:
            row['data'] = load(row.pop('data_json'))
        return ok({'documentId': document_id, 'documentVersion': doc['document_version'],
            'originalHash': doc['sha256'], 'readings': readings, 'candidates': candidates})


@router.post('/documents/{document_id}/candidate-reviews', response_model=Envelope)
def review(document_id: int, body: CandidateReview, request: Request, user=Depends(current_user)):
    from app.agent_review import confirm_candidate
    with request.app.state.db.connect(write=True) as con:
        return ok(confirm_candidate(con, user['id'], document_id, body.candidateId, body.fieldId,
            value=body.value, manual=body.manual, defer=body.defer, unit=body.unit))


@router.get('/documents/{document_id}/candidate-reviews', response_model=Envelope)
def review_history(document_id: int, request: Request, user=Depends(current_user)):
    from app.agent_review import confirmation_history
    with request.app.state.db.connect() as con:
        return ok({'items': confirmation_history(con, user['id'], document_id)})


@router.get('/agent-jobs/{job_id}', response_model=Envelope)
def get_job(job_id: str, request: Request, user=Depends(current_user)):
    return ok(request.app.state.service.store.job(user['id'], job_id))


@router.post('/agent-jobs/{job_id}/cancel', status_code=202, response_model=Envelope)
def cancel(job_id: str, request: Request, user=Depends(current_user)):
    request.app.state.service.store.cancel(user['id'], job_id)
    return ok(request.app.state.service.store.job(user['id'], job_id), 202)


@router.post('/agent-jobs/{job_id}/retry', status_code=202, response_model=Envelope)
def retry(job_id: str, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.store.retry(user['id'], job_id))


@router.post('/agent-results/{result_id}/explanations', status_code=202, response_model=Envelope)
def explain(result_id: str, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.enqueue_explanation(user['id'], result_id, idempotency_key=request.headers.get('Idempotency-Key')))


@router.post('/agent-results/{result_id}/inquiry', response_model=Envelope)
def inquiry(result_id: str, request: Request, body: InquiryRequest = InquiryRequest(), user=Depends(current_user)):
    try:
        return ok(request.app.state.service.inquiry(user['id'], result_id, question_ids=body.questionIds, text=body.text))
    except ValueError as exc:
        raise ApiError(422, 'INVALID_INQUIRY_SELECTION') from exc


@router.get('/agent-results/{result_id}/inquiries', response_model=Envelope)
def inquiries(result_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        request.app.state.service.store.result(con, user['id'], result_id)
        rows = con.execute('SELECT data_json FROM agent_inquiries WHERE result_id=? AND owner_id=? ORDER BY created_at',
            (result_id, user['id']))
        return ok({'items': [load(row['data_json']) for row in rows]})


@router.post('/agent-results/{result_id}/reports', status_code=202, response_model=Envelope)
def report(result_id: str, body: ReportRequest, request: Request, user=Depends(current_user)):
    return queued(request.app.state.service.enqueue_report(user['id'], result_id, body.explanationJobId,
        selected_document_ids=body.documentIds, idempotency_key=request.headers.get('Idempotency-Key')))


@router.get('/artifacts/{artifact_id}', response_class=FileResponse, responses={200:{'content':{'application/pdf':{'schema':{'type':'string','format':'binary'}},'application/zip':{'schema':{'type':'string','format':'binary'}}}}})
def download_artifact(artifact_id: str, request: Request, user=Depends(current_user)):
    path, row = request.app.state.service.store.artifact(user['id'], artifact_id)
    suffix = '.zip' if row['mime_type'] == 'application/zip' else '.pdf'
    return FileResponse(path, media_type=row['mime_type'], filename='wolgeup-result-' + artifact_id + suffix,
        headers={'Cache-Control': 'no-store'})


@router.delete('/artifacts/{artifact_id}', status_code=204)
def delete_artifact(artifact_id: str, request: Request, user=Depends(current_user)):
    path, _ = request.app.state.service.store.artifact(user['id'], artifact_id)
    with request.app.state.db.connect(write=True) as con:
        con.execute("UPDATE agent_artifacts SET status='DELETED' WHERE id=? AND owner_id=?", (artifact_id, user['id']))
    path.unlink(missing_ok=True)
    return Response(status_code=204)


@router.get('/agent-contracts', response_model=Envelope)
def contracts():
    from agent.schemas.public.inputs import InputSnapshot
    from agent.schemas.public.tasks import AgentTask
    from agent.rules.support_manifest import support_manifest
    from agent.rules.wages import default_rule_registry
    return ok({'task': AgentTask.model_json_schema(), 'snapshot': InputSnapshot.model_json_schema(), 'support': support_manifest(default_rule_registry())})


@router.get('/glossary', response_model=Envelope)
def glossary(q: str = ''):
    from agent.services.glossary import glossary_entries
    return ok({'items': glossary_entries(q)})
