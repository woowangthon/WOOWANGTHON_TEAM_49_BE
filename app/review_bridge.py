"""Publish an explicitly confirmed review into the existing case/group engine."""

import asyncio
from copy import deepcopy
import io

from agent.review.state import readiness, selection
from agent.schemas.public.results import ExtractionCandidate
from agent.services.extraction_profiles import statement_row_role
from agent.services.hashing import content_hash, digest
from app.agent_mapping import analysis_payload, snapshot_from_group
from app.db import dump, load, now
from app.errors import ApiError
from app.source_location import union_box
from app.integration_store import ensure_default_group, identifier, source_ref, owned_group
from app.security import case as owned_case, workplace as owned_workplace


def candidate_for_part(document_id, part, reading, page_count, settings):
    fields, evidence, projections = [], [], []
    for field in part['fields']:
        if field['extra']:
            continue
        confirmed = field['review_state'] == 'confirmed'
        ev = field.get('evidence') or {}
        eid = 'review-evidence:' + digest([document_id, field['id'], field.get('confirmation_id')])
        if confirmed:
            manual = field.get('source_kind') == 'user_statement'
            evidence.append({'evidence_id': eid, 'source_id': f'doc:{document_id}',
                'page': ev.get('page') or part['start_page'],
                'quote': '사용자 직접 입력: ' + field['value'] if manual else ev.get('quote', ''),
                'locator': 'review-field:' + field['id'], 'verification_method': 'manual_confirmed'})
        item = {'field_id': field['field_id'], 'raw_value': field.get('raw_value'),
            'normalized_value': field['value'] if confirmed else None, 'unit': field['unit'] if confirmed else None,
            'validation_status': 'valid' if confirmed else 'missing', 'source_page': ev.get('page'),
            'source_quote': ev.get('quote'), 'evidence_refs': [eid] if confirmed else [],
            'issues': [] if confirmed else ['USER_CONFIRMATION_REQUIRED']}
        fields.append(item)
        projections.append((field, item))
    methods = [p.get('method', 'python_ocr') for p in reading.get('pages', [])]
    candidate = ExtractionCandidate(document_id=str(document_id), document_version=1,
        document_type=part['document_type'], extraction_version='1.2',
        model_config_version=settings.model_config_version, fields=fields, evidence_catalog=evidence,
        issues=[], page_count=page_count, verification_method='ocr_unverified',
        recognized_pages=[p.get('text', '') for p in reading.get('pages', [])],
        page_methods=methods, source_method=methods[0] if methods and len(set(methods)) == 1 else 'mixed')
    return candidate, projections


def confirmed_input(state, exported, calculation_input, transaction_ids):
    """Use only reviewed fields. Never infer hours, calendars, totals or allocations."""
    result = {'profile': 'H-STD' if state['basic']['pay_type'] == 'HOURLY' else 'M-STD',
        'subjects_confirmed': state['options']['subjects_confirmed'], 'confirmed_fields': [],
        'evidence_catalog': [], 'earnings': [], 'deductions': [], 'condition_states': [],
        'earnings_complete': state['options']['earnings_complete'],
        'deductions_complete': state['options']['deductions_complete'],
        'payment_scope_confirmed': state['options']['payment_scope_confirmed'] and bool(transaction_ids),
        'selected_transaction_ids': transaction_ids, **deepcopy(calculation_input)}
    scalar_keys = {'contract': {'contract_rate'}, 'payslip': {'slip_rate', 'slip_minutes', 'gross', 'deduction', 'net'},
        'attendance': {'worked_minutes'}}
    scalars = {}
    for doc_id, part, candidate, projections in exported:
        result['evidence_catalog'].extend(candidate.model_dump(mode='json')['evidence_catalog'])
        by_id = {f['field_id']: (f, item) for f, item in projections}
        values = {key: f['value'] for key, (f, _) in by_id.items() if f['review_state'] == 'confirmed'}
        period_keys = ('contract_start', 'contract_end') if part['document_type'] == 'contract' else ('period_start', 'period_end')
        period = {'start': values[period_keys[0]], 'end': values[period_keys[1]]} if all(values.get(k) for k in period_keys) else None
        for key in scalar_keys.get(part['document_type'], set()):
            if key not in values:
                continue
            field, item = by_id[key]
            time_kind = state['options']['worked_time_kind' if key == 'worked_minutes' else 'slip_time_kind'] if field['unit'] == 'minute' else None
            scalars.setdefault(key, []).append({'field_id': key, 'value': field['value'], 'unit': field['unit'],
                'confirmation_id': field['confirmation_id'], 'evidence_refs': item['evidence_refs'],
                'period': period, 'time_kind': time_kind})
        if part['document_type'] == 'payslip':
            for prefix, target in (('earning', 'earnings'), ('deduction', 'deductions')):
                row_ids = {key.rsplit('_', 1)[0] for key in by_id if key.startswith(prefix + '_') and key.endswith('_amount')}
                for row_id in sorted(row_ids):
                    label, amount = by_id.get(row_id + '_label'), by_id.get(row_id + '_amount')
                    valid = bool(label and amount and label[0]['review_state'] == amount[0]['review_state'] == 'confirmed')
                    role = statement_row_role(label[0]['value']) if label and label[0]['review_state'] == 'confirmed' else 'detail'
                    if role == 'excluded':
                        continue
                    result[target].append({'item_id': f'review:{doc_id}:{row_id}',
                        'amount': amount[0]['value'] if valid else None, 'confirmed': valid,
                        'is_total': role in ('total', 'subtotal'),
                        'evidence_refs': list(dict.fromkeys([*label[1]['evidence_refs'], *amount[1]['evidence_refs']])) if label and amount else []})
                if not result[target] or any(not item['confirmed'] for item in result[target]):
                    result[target + '_complete'] = False
    for key, entries in scalars.items():
        if len(entries) == 1:
            result['confirmed_fields'].extend(entries)
        else:
            result['condition_states'].append({'field_id': key, 'state': 'conflict'})
    return result


async def complete_review(service, owner_id, rid, expected, payload):
    store, db = service.store, service.db
    with db.connect() as con:
        review = store.owned(con, owner_id, rid)
        store.version(review, expected)
        previous = con.execute('SELECT * FROM review_exports WHERE review_id=? AND revision=?', (rid, expected)).fetchone()
        if previous:
            if load(previous['snapshot_json'])['request'] != payload:
                raise ApiError(409, 'REVIEW_EXPORT_REQUEST_CHANGED')
            owned_case(con, previous['case_id'], owner_id)
            owned_group(con, previous['group_id'], owner_id)
            return {key: previous[key] for key in ('id', 'case_id', 'group_id', 'job_id', 'revision')}
        state = load(review['state_json'])
        documents = store.documents(con, rid)
        ready = readiness(state, documents)
        if ready['status'] != 'ready':
            raise ApiError(409, 'REVIEW_NOT_READY', details=ready)
        selected, _ = selection(state, documents)
    staged, prepared = [], []
    try:
        for document, part in selected:
            source, original = service.source(owner_id, rid, document['id'])
            filename = identifier() + ('.pdf' if source['media_type'] == 'application/pdf' else '.png' if source['media_type'] == 'image/png' else '.jpg')
            path = service.agent_service.store.data_dir / 'uploads' / filename
            await asyncio.to_thread(path.write_bytes, original)
            staged.append(path)
            derivative = None
            if source['media_type'] != 'application/pdf':
                def convert(content=original):
                    from PIL import Image, ImageOps
                    with Image.open(io.BytesIO(content)) as image:
                        image = ImageOps.exif_transpose(image).convert('RGB')
                        try:
                            output = io.BytesIO()
                            image.save(output, format='PDF', resolution=150)
                            return output.getvalue()
                        finally:
                            image.close()
                processed = await asyncio.to_thread(convert)
                derivative = {'storage_name': identifier() + '.pdf', 'content_hash': content_hash(processed)}
                derived_path = service.agent_service.store.data_dir / 'derived' / derivative['storage_name']
                await asyncio.to_thread(derived_path.write_bytes, processed)
                staged.append(derived_path)
            prepared.append((document, part, filename, derivative))
        # Recheck every original after slow conversion and immediately before publication.
        for document, _, _, _ in prepared:
            service.source(owner_id, rid, document['id'])
        with db.connect(write=True) as con:
            review = store.owned(con, owner_id, rid)
            store.version(review, expected)
            if con.execute('SELECT 1 FROM review_exports WHERE review_id=? AND revision=?', (rid, expected)).fetchone():
                raise ApiError(409, 'REVIEW_ALREADY_EXPORTED')
            basic = state['basic']
            target_case = owned_case(con, payload['case_id'], owner_id) if payload.get('case_id') else None
            wid = payload.get('workplace_id') or (target_case['workplace_id'] if target_case else None) or review['workplace_id']
            if wid:
                workplace = owned_workplace(con, wid, owner_id)
                from agent.services.identity import identity_key
                if identity_key(workplace['name'], company=True) != identity_key(basic['employer_name'], company=True):
                    raise ApiError(409, 'REVIEW_WORKPLACE_MISMATCH')
            else:
                wid = con.execute('''INSERT INTO workplaces(user_id,name,employment_status,created_at,updated_at)
                    VALUES(?,?,'CURRENT',?,?)''', (owner_id, basic['employer_name'], now(), now())).lastrowid
            cid = payload.get('case_id')
            if cid:
                case = owned_case(con, cid, owner_id)
                if case['workplace_id'] != wid or any(case[column] != basic[key] for column, key in
                    (('period_start', 'period_start'), ('period_end', 'period_end'), ('pay_type', 'pay_type'))):
                    raise ApiError(409, 'REVIEW_CASE_MISMATCH')
            else:
                cid = con.execute('''INSERT INTO cases(workplace_id,pay_type,period_start,period_end,scheduled_pay_date,updated_at)
                    VALUES(?,?,?,?,?,?)''', (wid, basic['pay_type'], basic['period_start'], basic['period_end'], basic['payment_date'] or None, now())).lastrowid
                ensure_default_group(con, cid)
            gid, export_id = identifier(), identifier()
            con.execute('INSERT INTO comparison_groups(id,case_id,name,created_at,updated_at) VALUES(?,?,?,?,?)',
                (gid, cid, state['title'], now(), now()))
            exported, document_ids = [], []
            for document, part, filename, derivative in prepared:
                did = con.execute('''INSERT INTO documents(workplace_id,document_type,file_name,storage_name,media_type,
                    sha256,page_count,processing_status,progress,created_at) VALUES(?,?,?,?,?,?,?,'REVIEW_REQUIRED',100,?)''',
                    (wid, part['document_type'].upper(), document['file_name'] + ' · ' + part['label'], filename,
                     document['media_type'], document['sha256'], document['page_count'], now())).lastrowid
                if derivative:
                    con.execute('''INSERT INTO document_derivatives(id,document_id,document_version,original_hash,
                        content_hash,storage_name,media_type,created_at) VALUES(?,?,1,?,?,?,'application/pdf',?)''',
                        (identifier(), did, document['sha256'], derivative['content_hash'], derivative['storage_name'], now()))
                con.execute('INSERT INTO document_links(case_id,document_id,review_status) VALUES(?,?,?)', (cid, did, 'CONFIRMED'))
                ref = source_ref(con, did, owner_id, cid)
                con.execute('INSERT INTO group_documents VALUES(?,?,?,?)', (gid, did, ref.document_version, ref.link_version))
                candidate, projections = candidate_for_part(did, part, document.get('reading') or {}, document['page_count'], service.settings)
                reading_id, candidate_id, job_id = identifier(), identifier(), identifier()
                reading = document.get('reading') or {}
                stored_reading = {'id': reading_id, 'document_id': str(did), 'document_version': 1,
                    'source_id': ref.source_id, 'content_hash': ref.content_hash,
                    'pages': [p.get('text', '') for p in reading.get('pages', [])] or [''] * document['page_count'],
                    'page_count': document['page_count'], 'page_methods': candidate.page_methods,
                    'source_method': candidate.source_method, 'image_pages': [False] * document['page_count']}
                con.execute('''INSERT INTO agent_jobs(id,owner_id,case_id,document_id,task_kind,payload_json,payload_hash,
                    idempotency_key,status,output_json,created_at,completed_at) VALUES(?,?,?,?,'read_document',?,?,?,'COMPLETED',?,?,?)''',
                    (job_id, owner_id, cid, did, dump({'source': ref.model_dump(mode='json')}), digest([rid, expected, part['id']]),
                     'review-import:' + str(did), dump({'ocr_result_id': reading_id, 'review_id': rid}), now(), now()))
                con.execute('INSERT INTO document_ocr VALUES(?,?,?,?,?,?,?,?)',
                    (reading_id, job_id, did, 1, ref.source_id, ref.content_hash, dump(stored_reading), now()))
                extraction_job_id = identifier()
                extraction_payload = {'source': ref.model_dump(mode='json'), 'ocr_result_id': reading_id,
                    'document_type': part['document_type'], 'extraction_method': 'text',
                    'target_fields': [], 'model_config_version': service.settings.model_config_version}
                con.execute('''INSERT INTO agent_jobs(id,owner_id,case_id,document_id,task_kind,payload_json,payload_hash,
                    idempotency_key,status,run_id,output_json,created_at,completed_at)
                    VALUES(?,?,?,?,'extract_document',?,?,?,'COMPLETED',?,?,?,?)''',
                    (extraction_job_id, owner_id, cid, did, dump(extraction_payload), digest(extraction_payload),
                     'review-candidate:' + str(did), identifier(),
                     dump({'candidate_id': candidate_id, 'candidate': candidate.model_dump(mode='json'), 'review_id': rid}), now(), now()))
                con.execute('''INSERT INTO extraction_candidates(id,job_id,document_id,document_version,ocr_result_id,
                    method,data_json,created_at) VALUES(?,?,?,1,?,'text',?,?)''',
                    (candidate_id, extraction_job_id, did, reading_id, candidate.model_dump_json(), now()))
                for field, item in projections:
                    confirmed = field['review_state'] == 'confirmed'
                    audit_id = identifier()
                    projection = {'candidateId': candidate_id, 'baseCandidateId': candidate_id,
                        'confirmationId': audit_id if confirmed else None, 'documentVersion': 1,
                        'documentHash': ref.content_hash, 'ocrResultId': reading_id,
                        'evidenceRefs': item['evidence_refs'], 'unit': field['unit'], 'reviewId': rid,
                        'reviewFieldId': field['id'], 'page': item['source_page'], 'text': item['source_quote'],
                        'boundingBoxes': (field.get('evidence') or {}).get('boxes', []),
                        'boundingBox': union_box((field.get('evidence') or {}).get('boxes', [])),
                        'coordinateSystem': 'RELATIVE_0_1'}
                    con.execute('''INSERT INTO fields(document_id,key,label,value_json,value_type,status,edited,confirmed_at,source_json)
                        VALUES(?,?,?,?,'TEXT',?,?,?,?)''', (did, field['field_id'], field['label'], dump(item['normalized_value']),
                        'CONFIRMED' if confirmed else 'UNCONFIRMED', int(confirmed), now() if confirmed else None, dump(projection)))
                    if confirmed:
                        audit = {'event': 'selected', 'review_id': rid, 'review_revision': expected,
                            'review_field': field, 'evidence_catalog': candidate.model_dump(mode='json')['evidence_catalog']}
                        con.execute('INSERT INTO document_confirmations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (audit_id, did, owner_id, field['field_id'], candidate_id, 1, ref.content_hash, reading_id, 'selected', dump(audit), now()))
                exported.append((did, part, candidate, projections))
                document_ids.append(did)
            confirmed = confirmed_input(state, exported, payload['calculation_input'], payload['selected_transaction_ids'])
            con.execute('UPDATE comparison_groups SET confirmed_json=? WHERE id=?', (dump(confirmed), gid))
            con.execute('INSERT INTO group_confirmations VALUES(?,?,?,?,?)', (identifier(), gid, 1, dump(confirmed), now()))
            con.execute('INSERT INTO review_exports VALUES(?,?,?,?,?,?,?,?)',
                (export_id, rid, expected, cid, gid, None, dump({'request': payload, 'state': state, 'documents': documents}), now()))
            for did, (document, part, _, _) in zip(document_ids, prepared):
                con.execute('INSERT INTO review_export_documents VALUES(?,?,?,?,?,?,?)',
                    (did, document['id'], export_id, part['id'], part['start_page'], part['end_page'], document['sha256']))
            # Use the same validator and frozen input path as the existing API.
            snapshot_from_group(con, gid, owner_id, 'review-validation')
            jid = None
            if payload['analyze']:
                snapshot = service.agent_service.store.freeze_snapshot(con, gid, owner_id)
                jid = service.agent_service.store.enqueue(con, owner_id, 'analyze_case', analysis_payload(snapshot),
                    case_id=cid, group_id=gid, snapshot_id=snapshot.snapshot_id, idempotency_key='review:' + export_id)
                con.execute('UPDATE review_exports SET job_id=? WHERE id=?', (jid, export_id))
            con.execute('UPDATE review_sessions SET workplace_id=?,case_id=? WHERE id=?', (wid, cid, rid))
            con.execute('INSERT INTO review_events VALUES(?,?,?,?,?,?,?)',
                (identifier(), rid, owner_id, expected, 'exported', dump({'export_id': export_id, 'group_id': gid}), now()))
        return {'id': export_id, 'case_id': cid, 'group_id': gid, 'job_id': jid, 'revision': expected}
    except BaseException:
        for path in staged:
            path.unlink(missing_ok=True)
        raise
