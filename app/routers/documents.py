import hashlib
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pypdf import PdfReader

from app.ai import validate_field_value
from app.db import dump, load, now
from app.engine import effective_fields, schedule
from app.errors import ApiError, ok
from app.outputs import API, DocumentDTO, FieldsDTO, Page
from app.routers.workspaces import page_data
from app.schemas import DocType, Execute, FieldPatch, LinkCreate, LinkPatch
from app.security import case, case_document, check_version, current_user, document, workplace

router = APIRouter(tags=['Documents & Review'])
MEDIA = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}


def doc_dto(con, row, case_id=None):
    links = con.execute('SELECT * FROM document_links WHERE document_id=?', (row['id'],)).fetchall()
    link = next((item for item in links if item['case_id'] == case_id), None)
    return {'documentId': row['id'], 'workplaceId': row['workplace_id'], 'documentType': row['document_type'],
        'fileName': row['file_name'], 'processingStatus': row['processing_status'], 'progress': row['progress'],
        'reviewStatus': link['review_status'] if link else 'NOT_STARTED', 'linkId': link['id'] if link else None,
        'linkedCaseIds': [item['case_id'] for item in links], 'uploadedAt': row['created_at'], 'errorCode': row['error_code'],
        'aiMetadata': load(row['ai_metadata'])}


def link_dto(row):
    return {'linkId': row['id'], 'caseId': row['case_id'], 'documentId': row['document_id'],
            'purpose': row['purpose'], 'applicableFrom': row['applicable_from'], 'applicableTo': row['applicable_to']}


def validate_period(start, end, case_row=None):
    if start and end and start > end:
        raise ApiError(422, 'INVALID_PERIOD')
    if case_row and ((start and start > case_row['period_end']) or (end and end < case_row['period_start'])):
        raise ApiError(422, 'INVALID_PERIOD', '문서 적용기간과 사례 기간이 겹치지 않습니다.')


async def save_upload(request, user, workplace_id, file, doc_type, case_id=None, applicable_from=None, applicable_to=None, replacement=None):
    state = request.app.state
    with state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        if case_id:
            c = case(con, case_id, user['id'])
            validate_period(applicable_from, applicable_to, c)
            if con.execute('SELECT COUNT(*) FROM document_links WHERE case_id=?', (case_id,)).fetchone()[0] >= 20:
                raise ApiError(409, 'DOCUMENT_LIMIT_EXCEEDED')
    validate_period(applicable_from, applicable_to)
    safe_name = ''.join(c for c in (file.filename or 'document').replace('\\', '/').split('/')[-1] if ord(c) >= 32 and ord(c) != 127)[:200]
    suffix = Path(safe_name).suffix.lower()
    if suffix not in MEDIA:
        raise ApiError(415, 'UNSUPPORTED_FILE_TYPE')
    storage_name = uuid.uuid4().hex + suffix
    path = state.settings.data_dir / 'uploads' / storage_name
    size = 0
    hashed = hashlib.sha256()
    try:
        with path.open('wb') as stream:
            while chunk := await file.read(64*1024):
                size += len(chunk)
                if size > state.settings.upload_max_bytes:
                    raise ApiError(413, 'FILE_TOO_LARGE')
                hashed.update(chunk)
                stream.write(chunk)
        if not size:
            raise ApiError(422, 'EMPTY_FILE')
        try:
            if suffix == '.pdf':
                with path.open('rb') as stream:
                    if stream.read(5) != b'%PDF-':
                        raise ValueError('invalid PDF signature')
                pdf = PdfReader(path)
                if pdf.is_encrypted:
                    raise ValueError('encrypted PDF is unsupported')
                page_count = len(pdf.pages)
                if not 1 <= page_count <= 50:
                    raise ApiError(422, 'PDF_PAGE_LIMIT_EXCEEDED')
            else:
                with Image.open(path) as image:
                    expected = 'PNG' if suffix == '.png' else 'JPEG'
                    if image.format != expected or max(image.size) > 10000:
                        raise ValueError('invalid image format or dimensions')
                    image.verify()
                page_count = 1
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(422, 'INVALID_FILE', '읽을 수 없는 파일입니다.') from exc
        with state.db.connect(write=True) as con:
            workplace(con, workplace_id, user['id'])
            if case_id:
                case(con, case_id, user['id'])
                if con.execute('SELECT COUNT(*) FROM document_links WHERE case_id=?', (case_id,)).fetchone()[0] >= 20:
                    raise ApiError(409, 'DOCUMENT_LIMIT_EXCEEDED')
            if replacement:
                rcid, original_id, expected = replacement
                check_version(case(con, rcid, user['id']), expected)
                case_document(con, rcid, original_id, user['id'])
            metadata = {'applicableFrom': applicable_from, 'applicableTo': applicable_to}
            did = con.execute('INSERT INTO documents(workplace_id,document_type,file_name,storage_name,media_type,sha256,page_count,ai_metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (workplace_id, doc_type, safe_name, storage_name, MEDIA[suffix], hashed.hexdigest(), page_count, dump(metadata), now())).lastrowid
            mutation = {}
            if case_id:
                con.execute('INSERT INTO document_links(case_id,document_id,applicable_from,applicable_to) VALUES(?,?,?,?)',
                            (case_id, did, applicable_from, applicable_to))
                mutation = schedule(con, case_id, delay=state.settings.recompute_debounce_seconds)
            if replacement:
                replacement_id = con.execute('INSERT INTO document_replacements(case_id,original_document_id,new_document_id,created_at) VALUES(?,?,?,?)',
                                              (rcid, original_id, did, now())).lastrowid
                mutation.update(replacementId=replacement_id, originalDocumentId=original_id,
                                caseId=rcid, originalLinkRetained=True, requiresConfirmation=True)
            duplicate_ids = [r[0] for r in con.execute('SELECT id FROM documents WHERE workplace_id=? AND sha256=? AND id!=? AND deleted_at IS NULL', (workplace_id, hashed.hexdigest(), did))]
        return {'documentId': did, 'processingStatus': 'UPLOADED', 'reviewStatus': 'NOT_STARTED',
                'progress': 10, 'duplicateCandidateIds': duplicate_ids, **mutation}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.post('/workplaces/{workplace_id}/documents', status_code=202)
async def upload_workplace(workplace_id: int, request: Request, file: UploadFile = File(...),
                           documentType: DocType = Form(...), source: Literal['USER_UPLOAD'] = Form('USER_UPLOAD'),
                           applicableFrom: date | None = Form(None), applicableTo: date | None = Form(None), user=Depends(current_user)):
    return ok(await save_upload(request, user, workplace_id, file, documentType,
                             applicable_from=str(applicableFrom) if applicableFrom else None,
                             applicable_to=str(applicableTo) if applicableTo else None), 202)


@router.post('/cases/{case_id}/documents', status_code=202)
async def upload_case(case_id: int, request: Request, file: UploadFile = File(...), documentType: DocType = Form(...),
                      source: Literal['USER_UPLOAD'] = Form('USER_UPLOAD'), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        c = case(con, case_id, user['id'])
    return ok(await save_upload(request, user, c['workplace_id'], file, documentType, case_id), 202)


@router.post('/cases/{case_id}/documents/{document_id}/replace', status_code=202)
async def replace_document(case_id: int, document_id: int, request: Request, file: UploadFile = File(...),
                           expectedInputVersion: int | None = Form(None), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        c = case(con, case_id, user['id'])
        check_version(c, expectedInputVersion)
        doc, _ = case_document(con, case_id, document_id, user['id'])
    return ok(await save_upload(request, user, c['workplace_id'], file, doc['document_type'],
                                replacement=(case_id, document_id, expectedInputVersion)), 202)


def replacement_row(con, case_id, replacement_id, user_id):
    c = case(con, case_id, user_id)
    row = con.execute('SELECT * FROM document_replacements WHERE id=? AND case_id=?', (replacement_id, case_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    original = document(con, c['workplace_id'], row['original_document_id'], user_id)
    candidate = document(con, c['workplace_id'], row['new_document_id'], user_id)
    return row, original, candidate


@router.get('/cases/{case_id}/document-replacements/{replacement_id}')
def get_replacement(case_id: int, replacement_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        row, original, candidate = replacement_row(con, case_id, replacement_id, user['id'])
        return ok({'replacementId': replacement_id, 'status': row['status'],
                   'original': doc_dto(con, original, case_id), 'candidate': doc_dto(con, candidate, case_id),
                   'fields': effective_fields(con, case_id, candidate['id'])})


@router.post('/cases/{case_id}/document-replacements/{replacement_id}/confirm')
def confirm_replacement(case_id: int, replacement_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    if not body.confirmed:
        raise ApiError(422, 'CONFIRMATION_REQUIRED')
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        row, original, candidate = replacement_row(con, case_id, replacement_id, user['id'])
        if row['status'] == 'CONFIRMED':
            return ok({'replacementId': replacement_id, 'status': 'CONFIRMED', 'documentId': candidate['id'], 'inputVersion': c['input_version']})
        check_version(c, body.expectedInputVersion)
        _, link = case_document(con, case_id, original['id'], user['id'])
        if candidate['processing_status'] not in ('READ_READY', 'READY', 'REVIEW_REQUIRED'):
            raise ApiError(409, 'DOCUMENT_STILL_PROCESSING' if candidate['processing_status'] in ('UPLOADED', 'PROCESSING') else 'DOCUMENT_ANALYSIS_FAILED')
        if con.execute('SELECT 1 FROM document_links WHERE case_id=? AND document_id=?', (case_id, candidate['id'])).fetchone():
            raise ApiError(409, 'DOCUMENT_ALREADY_LINKED')
        targets = {f['key']: f for f in con.execute('SELECT * FROM fields WHERE document_id=?', (candidate['id'],))}
        overrides = con.execute('''SELECT f.*,o.value_json AS override_value,o.status AS override_status,o.confirmed_at AS override_confirmed
            FROM fields f JOIN field_overrides o ON o.field_id=f.id WHERE f.document_id=? AND o.case_id=?''', (original['id'], case_id)).fetchall()
        conflicts = []
        for old in overrides:
            target = targets.get(old['key'])
            if target is None:
                fid = con.execute('INSERT INTO fields(document_id,key,label,value_json,value_type,status) VALUES(?,?,?,?,?,?)',
                    (candidate['id'], old['key'], old['label'], 'null', old['value_type'], 'MISSING')).lastrowid
                targets[old['key']] = con.execute('SELECT * FROM fields WHERE id=?', (fid,)).fetchone()
            elif target['value_type'] != old['value_type']:
                conflicts.append({'fieldId': old['id'], 'key': old['key'], 'reason': 'MISSING_OR_CHANGED_TYPE'})
        if conflicts:
            raise ApiError(409, 'REPLACEMENT_OVERRIDE_CONFLICT', '기존 Case 수정값을 새 문서에 보존할 수 없습니다. 원문을 검토해 주세요.', details={'fields': conflicts})
        for old in overrides:
            target = targets[old['key']]
            source = {'documentId': candidate['id'], 'page': None, 'boundingBox': None,
                      'coordinateSystem': 'RELATIVE_0_1', 'carriedFromDocumentId': original['id'],
                      'carriedFromFieldId': old['id'], 'text': '이전 문서의 Case 수정값을 유지했습니다. 새 원문과 다시 대조하세요.'}
            con.execute('INSERT INTO field_overrides(case_id,field_id,value_json,status,confirmed_at,source_json) VALUES(?,?,?,?,?,?)',
                        (case_id, target['id'], old['override_value'], 'UNCONFIRMED', None, dump(source)))
            con.execute('INSERT INTO field_history(field_id,case_id,user_id,previous_json,new_json,created_at) VALUES(?,?,?,?,?,?)',
                        (target['id'], case_id, user['id'], dump({'value': load(target['value_json']), 'status': target['status']}),
                         dump({'value': load(old['override_value']), 'status': 'UNCONFIRMED', 'origin': 'REPLACEMENT_CARRY_FORWARD', 'originalDocumentId': original['id']}), now()))
        con.execute("UPDATE document_links SET document_id=?,link_version=link_version+1,review_status='IN_PROGRESS' WHERE id=?", (candidate['id'], link['id']))
        selected = con.execute('''SELECT gd.group_id FROM group_documents gd JOIN comparison_groups g ON g.id=gd.group_id
            WHERE g.case_id=? AND gd.document_id=?''', (case_id, original['id'])).fetchall()
        for item in selected:
            con.execute('DELETE FROM group_documents WHERE group_id=? AND document_id IN (?,?)',
                (item['group_id'], original['id'], candidate['id']))
            con.execute('INSERT INTO group_documents(group_id,document_id,document_version,link_version) VALUES(?,?,?,?)',
                (item['group_id'], candidate['id'], candidate['document_version'], link['link_version']+1))
            con.execute('UPDATE comparison_groups SET needs_review=1,version=version+1,updated_at=? WHERE id=?', (now(), item['group_id']))
        con.execute("UPDATE document_replacements SET status='CONFIRMED',confirmed_at=? WHERE id=?", (now(), replacement_id))
        return ok({'replacementId': replacement_id, 'status': 'CONFIRMED', 'documentId': candidate['id'],
                   'preservedOverrideCount': len(overrides), 'linkId': link['id'], 'originalRetained': True,
                   **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)})


@router.get('/documents', response_model=API[Page[DocumentDTO]])
def library(request: Request, workplaceId: int | None = None, documentType: DocType | None = None,
            page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        if workplaceId:
            workplace(con, workplaceId, user['id'])
        rows = con.execute('''SELECT d.*,w.name AS workplace_name FROM documents d JOIN workplaces w ON w.id=d.workplace_id
            WHERE w.user_id=? AND w.deleted_at IS NULL AND d.deleted_at IS NULL
            AND (? IS NULL OR d.workplace_id=?) AND (? IS NULL OR d.document_type=?) ORDER BY d.id DESC''',
            (user['id'], workplaceId, workplaceId, documentType, documentType)).fetchall()
        return ok(page_data([{**doc_dto(con, row), 'workplaceName': row['workplace_name']} for row in rows], page, size))


@router.get('/workplaces/{workplace_id}/documents', response_model=API[Page[DocumentDTO]])
def workplace_documents(workplace_id: int, request: Request, page: int = Query(1, ge=1),
                        size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    return library(request, workplace_id, None, page, size, user)


@router.get('/cases/{case_id}/documents', response_model=API[Page[DocumentDTO]])
def case_documents(case_id: int, request: Request, page: int = Query(1, ge=1),
                   size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        rows = con.execute('SELECT d.* FROM documents d JOIN document_links l ON l.document_id=d.id WHERE l.case_id=? AND d.deleted_at IS NULL ORDER BY d.id', (case_id,)).fetchall()
        return ok(page_data([doc_dto(con, row, case_id) for row in rows], page, size))


@router.get('/workplaces/{workplace_id}/documents/{document_id}', response_model=API[DocumentDTO])
def original(workplace_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(doc_dto(con, document(con, workplace_id, document_id, user['id'])))


@router.get('/cases/{case_id}/documents/{document_id}', response_model=API[DocumentDTO])
def case_original(case_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc, _ = case_document(con, case_id, document_id, user['id'])
        return ok(doc_dto(con, doc, case_id))


@router.get('/workplaces/{workplace_id}/documents/{document_id}/preview')
def preview(workplace_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc = document(con, workplace_id, document_id, user['id'])
        return ok({'documentId': document_id, 'mediaType': doc['media_type'], 'pageCount': doc['page_count'],
            'previewUrl': f'/api/v1/workplaces/{workplace_id}/documents/{document_id}/file',
            'coordinateSystem': 'RELATIVE_0_1', 'rotationDegrees': 0, 'supportsHighlight': any(
                (load(f[0]) or {}).get('boundingBox') for f in con.execute('SELECT source_json FROM fields WHERE document_id=?', (document_id,)))})


@router.get('/workplaces/{workplace_id}/documents/{document_id}/file', response_class=FileResponse,
            responses={200: {'content': {'application/pdf': {}, 'image/png': {}, 'image/jpeg': {}}}})
def download_original(workplace_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc = document(con, workplace_id, document_id, user['id'])
    return FileResponse(request.app.state.settings.data_dir / 'uploads' / doc['storage_name'], media_type=doc['media_type'],
                        filename=doc['file_name'], content_disposition_type='inline', headers={'Cache-Control': 'no-store'})


@router.post('/cases/{case_id}/document-links', status_code=201)
def link_document(case_id: int, body: LinkCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        doc = document(con, c['workplace_id'], body.documentId, user['id'])
        metadata = load(doc['ai_metadata']) or {}
        start = (str(body.applicableFrom) if body.applicableFrom else None) if 'applicableFrom' in body.model_fields_set else metadata.get('applicableFrom')
        end = (str(body.applicableTo) if body.applicableTo else None) if 'applicableTo' in body.model_fields_set else metadata.get('applicableTo')
        validate_period(start, end, c)
        if con.execute('SELECT COUNT(*) FROM document_links WHERE case_id=?', (case_id,)).fetchone()[0] >= 20:
            raise ApiError(409, 'DOCUMENT_LIMIT_EXCEEDED')
        try:
            lid = con.execute('INSERT INTO document_links(case_id,document_id,purpose,applicable_from,applicable_to) VALUES(?,?,?,?,?)',
                              (case_id, body.documentId, body.purpose, start, end)).lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, 'DOCUMENT_ALREADY_LINKED')
        return ok({'linkId': lid, 'caseId': case_id, 'documentId': body.documentId, 'overrideCount': 0,
                   **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)}, 201)


@router.patch('/cases/{case_id}/document-links/{link_id}')
def patch_link(case_id: int, link_id: int, body: LinkPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        row = con.execute('SELECT * FROM document_links WHERE id=? AND case_id=?', (link_id, case_id)).fetchone()
        if row is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        values = body.model_dump(mode='json', exclude_unset=True, exclude={'expectedInputVersion'})
        validate_period(values.get('applicableFrom', row['applicable_from']), values.get('applicableTo', row['applicable_to']), c)
        names = {'purpose': 'purpose', 'applicableFrom': 'applicable_from', 'applicableTo': 'applicable_to'}
        for key, value in values.items():
            con.execute(f'UPDATE document_links SET {names[key]}=? WHERE id=?', (value, link_id))
        if values:
            con.execute('UPDATE document_links SET link_version=link_version+1 WHERE id=?', (link_id,))
        return ok({'linkId': link_id, **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)})


@router.delete('/cases/{case_id}/document-links/{link_id}', status_code=204)
def unlink(case_id: int, link_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        link = con.execute('SELECT * FROM document_links WHERE id=? AND case_id=?', (link_id, case_id)).fetchone()
        if link is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        con.execute('DELETE FROM document_links WHERE id=?', (link_id,))
        # Keep overrides/history so that re-linking cannot silently discard confirmed edits.
        schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


def document_payment_scope(con, workplace_id, document_id):
    transaction_ids = []
    case_ids = set()
    for row in con.execute('SELECT id,provenance_json FROM payment_transactions WHERE workplace_id=?', (workplace_id,)):
        provenance = load(row['provenance_json']) or {}
        if provenance.get('kind') == 'document' and str(provenance.get('document_id')) == str(document_id):
            transaction_ids.append(row['id'])
            case_ids.update(item[0] for item in con.execute('SELECT case_id FROM allocations WHERE transaction_id=?', (row['id'],)))
    return transaction_ids, case_ids


@router.get('/workplaces/{workplace_id}/documents/{document_id}/deletion-impact')
def document_impact(workplace_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        document(con, workplace_id, document_id, user['id'])
        ids = [r[0] for r in con.execute('SELECT case_id FROM document_links WHERE document_id=?', (document_id,))]
        transaction_ids, allocation_cases = document_payment_scope(con, workplace_id, document_id)
        return ok({'documentId': document_id, 'affectedCaseIds': sorted(set(ids) | allocation_cases),
            'linkedCaseCount': len(ids), 'derivedTransactionIds': transaction_ids,
            'downloadedExternalCopiesCannotBeRecalled': True})


@router.delete('/workplaces/{workplace_id}/documents/{document_id}', status_code=204)
def delete_document(workplace_id: int, document_id: int, request: Request, confirm: bool = False, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        document(con, workplace_id, document_id, user['id'])
        ids = [r[0] for r in con.execute('SELECT case_id FROM document_links WHERE document_id=?', (document_id,))]
        transaction_ids, allocation_cases = document_payment_scope(con, workplace_id, document_id)
        ids = sorted(set(ids) | allocation_cases)
        if ids and not confirm:
            raise ApiError(409, 'DOCUMENT_SHARED_REFERENCES_EXIST', details={'affectedCaseIds': ids, 'confirmationQuery': 'confirm=true'})
        con.execute('UPDATE documents SET deleted_at=? WHERE id=?', (now(), document_id))
        con.execute('DELETE FROM document_links WHERE document_id=?', (document_id,))
        for transaction_id in transaction_ids:
            con.execute('DELETE FROM allocations WHERE transaction_id=?', (transaction_id,))
            con.execute('DELETE FROM payment_transactions WHERE id=?', (transaction_id,))
        for cid in ids:
            schedule(con, cid, delay=request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


@router.post('/cases/{case_id}/documents/{document_id}/retry', status_code=202)
def retry_document(case_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        doc, _ = case_document(con, case_id, document_id, user['id'])
        if doc['processing_status'] != 'FAILED':
            raise ApiError(409, 'DOCUMENT_NOT_FAILED')
        con.execute("UPDATE documents SET processing_status='UPLOADED',progress=10,error_code=NULL WHERE id=?", (document_id,))
    return ok({'documentId': document_id, 'processingStatus': 'UPLOADED'}, 202)


@router.get('/cases/{case_id}/documents/{document_id}/fields', response_model=API[FieldsDTO])
def fields(case_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc, link = case_document(con, case_id, document_id, user['id'])
        return ok({'documentId': document_id, 'caseId': case_id, 'processingStatus': doc['processing_status'],
                   'reviewStatus': link['review_status'], 'fields': effective_fields(con, case_id, document_id)})


def update_field(con, doc, field_id, body, user_id, settings, case_id=None):
    if doc['processing_status'] in ('UPLOADED', 'PROCESSING'):
        raise ApiError(409, 'DOCUMENT_STILL_PROCESSING')
    field = con.execute('SELECT * FROM fields WHERE id=? AND document_id=?', (field_id, doc['id'])).fetchone()
    if field is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    if (load(field['source_json']) or {}).get('candidateId'):
        raise ApiError(409, 'USE_CANDIDATE_REVIEW_ENDPOINT',
            '추출 후보의 확인은 candidate-reviews, 비교 입력의 수정은 comparison-groups의 confirmed-input을 사용하세요.')
    override = con.execute('SELECT * FROM field_overrides WHERE case_id=? AND field_id=?', (case_id, field_id)).fetchone() if case_id else None
    active = override or field
    previous = load(active['value_json'])
    value = body.value if 'value' in body.model_fields_set else previous
    try:
        validate_field_value(field['value_type'], value)
    except ValueError as exc:
        raise ApiError(422, 'INVALID_FIELD_VALUE', str(exc))
    confirmed = body.confirmed is True
    status = 'MISSING' if value is None else ('CONFIRMED' if confirmed else 'UNCONFIRMED')
    timestamp = now() if status == 'CONFIRMED' else None
    if case_id:
        source_json = override['source_json'] if override and 'value' not in body.model_fields_set else None
        con.execute('INSERT INTO field_overrides(case_id,field_id,value_json,status,confirmed_at,source_json) VALUES(?,?,?,?,?,?) ON CONFLICT(case_id,field_id) DO UPDATE SET value_json=excluded.value_json,status=excluded.status,confirmed_at=excluded.confirmed_at,source_json=excluded.source_json',
                    (case_id, field_id, dump(value), status, timestamp, source_json))
        affected = [case_id]
    else:
        if body.expectedInputVersion is not None:
            raise ApiError(422, 'CASE_VERSION_NOT_APPLICABLE', '공유 필드 수정에는 expectedInputVersion을 보내지 마세요.')
        con.execute('UPDATE fields SET value_json=?,status=?,edited=1,confirmed_at=? WHERE id=?', (dump(value), status, timestamp, field_id))
        affected = [r[0] for r in con.execute('''SELECT l.case_id FROM document_links l WHERE l.document_id=?
            AND NOT EXISTS (SELECT 1 FROM field_overrides o WHERE o.case_id=l.case_id AND o.field_id=?)''', (doc['id'], field_id))]
    con.execute('INSERT INTO field_history(field_id,case_id,user_id,previous_json,new_json,created_at) VALUES(?,?,?,?,?,?)',
                (field_id, case_id, user_id, dump({'value': previous, 'status': active['status']}), dump({'value': value, 'status': status}), now()))
    mutations = {}
    for cid in affected:
        con.execute("UPDATE document_links SET review_status='IN_PROGRESS' WHERE case_id=? AND document_id=?", (cid, doc['id']))
        mutations[str(cid)] = schedule(con, cid, delay=settings.recompute_debounce_seconds)
    return {'fieldId': field_id, 'previousValue': previous, 'value': value, 'status': status, 'editedByUser': True,
        'hasCaseOverride': bool(case_id), 'effectiveValueOrigin': 'CASE_OVERRIDE' if case_id else 'SHARED_DEFAULT',
        'affectedCaseIds': affected, 'caseUpdates': mutations, **(mutations.get(str(case_id), {}) if case_id else {})}


@router.patch('/cases/{case_id}/documents/{document_id}/fields/{field_id}')
def patch_case_field(case_id: int, document_id: int, field_id: int, body: FieldPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        doc, _ = case_document(con, case_id, document_id, user['id'])
        return ok(update_field(con, doc, field_id, body, user['id'], request.app.state.settings, case_id))


@router.patch('/workplaces/{workplace_id}/documents/{document_id}/fields/{field_id}')
def patch_shared_field(workplace_id: int, document_id: int, field_id: int, body: FieldPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        doc = document(con, workplace_id, document_id, user['id'])
        return ok(update_field(con, doc, field_id, body, user['id'], request.app.state.settings))


@router.delete('/cases/{case_id}/documents/{document_id}/field-overrides/{field_id}')
def remove_override(case_id: int, document_id: int, field_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case_document(con, case_id, document_id, user['id'])
        field = con.execute('SELECT id FROM fields WHERE id=? AND document_id=?', (field_id, document_id)).fetchone()
        if field is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        con.execute('DELETE FROM field_overrides WHERE case_id=? AND field_id=?', (case_id, field_id))
        con.execute("UPDATE document_links SET review_status='IN_PROGRESS' WHERE case_id=? AND document_id=?", (case_id, document_id))
        return ok({'fieldId': field_id, **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)})


@router.get('/cases/{case_id}/documents/{document_id}/fields/{field_id}/history')
def history(case_id: int, document_id: int, field_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case_document(con, case_id, document_id, user['id'])
        if con.execute('SELECT id FROM fields WHERE id=? AND document_id=?', (field_id, document_id)).fetchone() is None:
            raise ApiError(404, 'RESOURCE_NOT_FOUND')
        items = [{'historyId': r['id'], 'scope': 'SHARED_DEFAULT' if r['case_id'] is None else 'CASE_OVERRIDE',
                  'previous': load(r['previous_json']), 'new': load(r['new_json']), 'createdAt': r['created_at']}
                 for r in con.execute('SELECT * FROM field_history WHERE field_id=? AND (case_id IS NULL OR case_id=?) ORDER BY id', (field_id, case_id))]
        return ok({'items': items})


@router.get('/cases/{case_id}/documents/{document_id}/review')
def review(case_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        doc, link = case_document(con, case_id, document_id, user['id'])
        pending = [f['fieldId'] for f in effective_fields(con, case_id, document_id) if f['status'] != 'CONFIRMED']
        return ok({'documentId': document_id, 'reviewStatus': link['review_status'], 'unconfirmedFieldIds': pending,
                   'processingStatus': doc['processing_status']})


@router.post('/cases/{case_id}/documents/{document_id}/review/confirm')
def confirm_review(case_id: int, document_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        doc, _ = case_document(con, case_id, document_id, user['id'])
        if doc['processing_status'] in ('UPLOADED', 'PROCESSING'):
            raise ApiError(409, 'DOCUMENT_STILL_PROCESSING')
        values = effective_fields(con, case_id, document_id)
        pending = [f['fieldId'] for f in values if f['status'] != 'CONFIRMED']
        status = 'CONFIRMED' if values and not pending else 'IN_PROGRESS'
        con.execute('UPDATE document_links SET review_status=? WHERE case_id=? AND document_id=?', (status, case_id, document_id))
        return ok({'documentId': document_id, 'reviewStatus': status, 'unconfirmedFieldIds': pending})
