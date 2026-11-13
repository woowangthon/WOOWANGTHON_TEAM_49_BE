from app.schemas import Envelope
import sqlite3

from fastapi import APIRouter, Depends, Query, Request, Response

from app.engine import latest, schedule, snapshot_dto
from app.db import dump, load
from app.errors import ApiError, ok
from app.idempotency import idempotency_header, remember, replay
from app.routers.analysis import snapshot_list
from app.routers.workspaces import page_data
from app.schemas import AllocationCreate, AllocationPatch, TransactionCreate, TransactionPatch
from app.security import case, check_version, current_user, workplace

router = APIRouter(tags=['Payments'])


def transaction(con, workplace_id, transaction_id):
    row = con.execute('SELECT * FROM payment_transactions WHERE id=? AND workplace_id=?', (transaction_id, workplace_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def transaction_dto(con, row):
    allocated = con.execute('SELECT COALESCE(SUM(allocated_amount),0) FROM allocations WHERE transaction_id=?', (row['id'],)).fetchone()[0]
    return {'transactionId': row['id'], 'workplaceId': row['workplace_id'], 'paymentMethod': row['payment_method'],
        'transactionDate': row['transaction_date'], 'amount': row['amount'], 'sender': row['sender'], 'memo': row['memo'],
        'source': row['source'], 'confirmationStatus': row['confirmation_status'], 'totalAllocatedAcrossCases': allocated,
        'remainingUnallocated': row['amount'] - allocated,
        'provenance': load(row['provenance_json'])}


def insert_transaction(con, workplace_id, data):
    from app.agent_mapping import prepare_transaction_provenance, same_document_row
    provenance = prepare_transaction_provenance(con, workplace_id, data)
    for existing in con.execute('SELECT id,provenance_json FROM payment_transactions WHERE workplace_id=?', (workplace_id,)):
        previous = load(existing['provenance_json']) or {}
        same_reference = provenance.get('bank_reference') and provenance.get('bank_reference') == previous.get('bank_reference')
        same_row = same_document_row(provenance, previous)
        if same_reference or same_row:
            raise ApiError(409, 'DUPLICATE_TRANSACTION', details={'existingTransactionId': existing['id']})
    return con.execute('''INSERT INTO payment_transactions(workplace_id,payment_method,transaction_date,amount,sender,memo,source,confirmation_status,provenance_json)
        VALUES(?,?,?,?,?,?,?,?,?)''', (workplace_id, data['paymentMethod'], data['transactionDate'], data['amount'],
        data.get('sender'), data.get('memo'), data.get('source', 'USER_INPUT'), data.get('confirmationStatus', 'USER_REPORTED'),
        dump(provenance))).lastrowid


def insert_allocation(con, case_id, transaction_id, amount):
    c = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    tx = transaction(con, c['workplace_id'], transaction_id)
    used = con.execute('SELECT COALESCE(SUM(allocated_amount),0) FROM allocations WHERE transaction_id=?', (transaction_id,)).fetchone()[0]
    if used + amount > tx['amount']:
        raise ApiError(409, 'ALLOCATION_EXCEEDS_TRANSACTION')
    try:
        aid = con.execute('INSERT INTO allocations(case_id,transaction_id,allocated_amount) VALUES(?,?,?)', (case_id, transaction_id, amount)).lastrowid
    except sqlite3.IntegrityError:
        raise ApiError(409, 'DUPLICATE_ALLOCATION')
    return {'allocationId': aid, 'caseId': case_id, 'transactionId': transaction_id, 'allocatedAmount': amount,
            'transactionAmount': tx['amount'], 'totalAllocatedAcrossCases': used + amount, 'remainingUnallocated': tx['amount'] - used - amount}


@router.post('/workplaces/{workplace_id}/payment-transactions', status_code=201, response_model=Envelope)
def create_transaction(workplace_id: int, body: TransactionCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        tid = insert_transaction(con, workplace_id, body.model_dump(mode='json'))
        return ok(transaction_dto(con, transaction(con, workplace_id, tid)), 201)


@router.get('/workplaces/{workplace_id}/payment-transactions', response_model=Envelope)
def list_transactions(workplace_id: int, request: Request, page: int = Query(1, ge=1),
                      size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        rows = con.execute('SELECT * FROM payment_transactions WHERE workplace_id=? ORDER BY id DESC', (workplace_id,)).fetchall()
        return ok(page_data([transaction_dto(con, row) for row in rows], page, size))


@router.get('/workplaces/{workplace_id}/payment-transactions/duplicate-candidates', response_model=Envelope)
def duplicate_candidates(workplace_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        pairs = con.execute('''SELECT a.id,b.id FROM payment_transactions a JOIN payment_transactions b ON a.id<b.id
            AND a.workplace_id=b.workplace_id AND a.amount=b.amount AND a.transaction_date=b.transaction_date
            AND a.payment_method=b.payment_method WHERE a.workplace_id=?''', (workplace_id,)).fetchall()
        return ok({'candidates': [{'transactionIds': list(pair), 'reason': 'SAME_DATE_AMOUNT_METHOD'} for pair in pairs]})


@router.get('/workplaces/{workplace_id}/deposit-candidates', response_model=Envelope)
def deposit_candidates(workplace_id: int, request: Request, user=Depends(current_user)):
    """Expose current extracted rows for explicit ledger registration and confirmation."""
    import re
    from app.agent_mapping import candidate_value, prepare_transaction_provenance, same_document_row
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        items = []
        for doc in con.execute("SELECT * FROM documents WHERE workplace_id=? AND document_type='DEPOSIT' AND deleted_at IS NULL", (workplace_id,)):
            candidate = con.execute("SELECT * FROM extraction_candidates WHERE document_id=? AND document_version=? AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1", (doc['id'], doc['document_version'])).fetchone()
            if not candidate:
                continue
            fields = load(candidate['data_json'])['fields']
            rows = sorted({m[1] for f in fields if (m := re.fullmatch(r'(transaction_[1-9]\d*)_deposit_amount', f['field_id']))})
            for row_id in rows:
                values = {f['field_id'].removeprefix(row_id + '_'): candidate_value(f) for f in fields if f['field_id'].startswith(row_id + '_')}
                body = {'source': 'DOCUMENT', 'sourceDocumentId': doc['id'], 'sourceCandidateId': candidate['id'],
                        'sourceRowId': row_id, 'paymentMethod': 'BANK_TRANSFER', 'transactionDate': values.get('date'),
                        'amount': values.get('deposit_amount'), 'confirmationStatus': 'USER_REPORTED'}
                reason, existing_id = None, None
                try:
                    provenance = prepare_transaction_provenance(con, workplace_id, body)
                    for tx in con.execute('SELECT id,provenance_json FROM payment_transactions WHERE workplace_id=?', (workplace_id,)):
                        previous = load(tx['provenance_json']) or {}
                        if same_document_row(provenance, previous) or provenance.get('bank_reference') and provenance.get('bank_reference') == previous.get('bank_reference'):
                            existing_id = tx['id']
                except ApiError as exc:
                    reason = exc.code
                items.append({'registration': body, 'registerable': reason is None and existing_id is None,
                              'reasonCode': reason, 'existingTransactionId': existing_id,
                              'requiresUserConfirmation': True, 'requiresCaseAllocation': True})
        return ok({'items': items})


@router.get('/workplaces/{workplace_id}/payment-transactions/{transaction_id}', response_model=Envelope)
def get_transaction(workplace_id: int, transaction_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        workplace(con, workplace_id, user['id'])
        dto = transaction_dto(con, transaction(con, workplace_id, transaction_id))
        dto['allocations'] = [{'allocationId': r['id'], 'caseId': r['case_id'], 'allocatedAmount': r['allocated_amount']}
                              for r in con.execute('SELECT * FROM allocations WHERE transaction_id=?', (transaction_id,))]
        return ok(dto)


@router.patch('/workplaces/{workplace_id}/payment-transactions/{transaction_id}', response_model=Envelope)
def patch_transaction(workplace_id: int, transaction_id: int, body: TransactionPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        tx = transaction(con, workplace_id, transaction_id)
        dto = transaction_dto(con, tx)
        values = body.model_dump(mode='json', exclude_unset=True)
        if tx['source'] == 'DOCUMENT' and set(values) & {'amount', 'paymentMethod', 'transactionDate', 'sender', 'bankReference'}:
            raise ApiError(409, 'DOCUMENT_TRANSACTION_IMMUTABLE', '문서 거래의 원문 값은 변경할 수 없습니다. 수정된 수기 기록을 별도로 등록하세요.')
        if 'bankReference' in values:
            provenance = load(tx['provenance_json']) or {}
            reference = values.pop('bankReference')
            if reference is not None and not reference.strip():
                raise ApiError(422, 'INVALID_BANK_REFERENCE')
            provenance['bank_reference'] = reference.strip() if reference else None
            if provenance['bank_reference']:
                for existing in con.execute('SELECT id,provenance_json FROM payment_transactions WHERE workplace_id=? AND id!=?', (workplace_id, transaction_id)):
                    if (load(existing['provenance_json']) or {}).get('bank_reference') == provenance['bank_reference']:
                        raise ApiError(409, 'DUPLICATE_TRANSACTION', details={'existingTransactionId': existing['id']})
            con.execute('UPDATE payment_transactions SET provenance_json=? WHERE id=?', (dump(provenance), transaction_id))
        for key in ('amount', 'paymentMethod', 'transactionDate', 'confirmationStatus'):
            if key in values and values[key] is None:
                raise ApiError(422, 'INVALID_INPUT')
        if values.get('amount', tx['amount']) < dto['totalAllocatedAcrossCases']:
            raise ApiError(409, 'ALLOCATION_EXCEEDS_TRANSACTION')
        names = {'amount': 'amount', 'paymentMethod': 'payment_method', 'transactionDate': 'transaction_date',
                 'sender': 'sender', 'memo': 'memo', 'confirmationStatus': 'confirmation_status'}
        for key, value in values.items():
            con.execute(f'UPDATE payment_transactions SET {names[key]}=? WHERE id=?', (value, transaction_id))
        affected = [r[0] for r in con.execute('SELECT case_id FROM allocations WHERE transaction_id=?', (transaction_id,))]
        updates = {str(cid): schedule(con, cid, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds) for cid in affected}
        return ok({**transaction_dto(con, transaction(con, workplace_id, transaction_id)), 'affectedCaseIds': affected, 'caseUpdates': updates})


@router.delete('/workplaces/{workplace_id}/payment-transactions/{transaction_id}', status_code=204)
def delete_transaction(workplace_id: int, transaction_id: int, request: Request, confirm: bool = False, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        workplace(con, workplace_id, user['id'])
        transaction(con, workplace_id, transaction_id)
        affected = [r[0] for r in con.execute('SELECT case_id FROM allocations WHERE transaction_id=?', (transaction_id,))]
        if affected and not confirm:
            raise ApiError(409, 'TRANSACTION_HAS_ALLOCATIONS', details={'affectedCaseIds': affected, 'confirmationQuery': 'confirm=true'})
        con.execute('DELETE FROM allocations WHERE transaction_id=?', (transaction_id,))
        con.execute('DELETE FROM payment_transactions WHERE id=?', (transaction_id,))
        for cid in affected:
            schedule(con, cid, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


@router.post('/cases/{case_id}/deposits', status_code=201, dependencies=[Depends(idempotency_header)], response_model=Envelope)
def allocate(case_id: int, body: AllocationCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        cached = replay(con, request, user['id'], body)
        if cached is not None:
            return cached
        check_version(c, body.expectedInputVersion)
        return remember(con, request, user['id'], body, {**insert_allocation(con, case_id, body.transactionId, body.allocatedAmount),
            **schedule(con, case_id, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds)}, 201)


@router.get('/cases/{case_id}/deposits', response_model=Envelope)
def allocations(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        rows = con.execute('SELECT a.*,t.payment_method FROM allocations a JOIN payment_transactions t ON t.id=a.transaction_id WHERE a.case_id=? ORDER BY a.id', (case_id,)).fetchall()
        return ok(page_data([{'allocationId': r['id'], 'transactionId': r['transaction_id'],
                             'allocatedAmount': r['allocated_amount'], 'paymentMethod': r['payment_method']} for r in rows], page, size))


def allocation(con, case_id, allocation_id):
    row = con.execute('SELECT * FROM allocations WHERE id=? AND case_id=?', (allocation_id, case_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


@router.patch('/cases/{case_id}/deposits/{allocation_id}', response_model=Envelope)
def patch_allocation(case_id: int, allocation_id: int, body: AllocationPatch, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user['id'])
        check_version(c, body.expectedInputVersion)
        row = allocation(con, case_id, allocation_id)
        tx = transaction(con, c['workplace_id'], row['transaction_id'])
        used = con.execute('SELECT COALESCE(SUM(allocated_amount),0) FROM allocations WHERE transaction_id=? AND id!=?', (tx['id'], allocation_id)).fetchone()[0]
        if used + body.allocatedAmount > tx['amount']:
            raise ApiError(409, 'ALLOCATION_EXCEEDS_TRANSACTION')
        con.execute('UPDATE allocations SET allocated_amount=? WHERE id=?', (body.allocatedAmount, allocation_id))
        return ok({'allocationId': allocation_id, 'allocatedAmount': body.allocatedAmount,
                   **schedule(con, case_id, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds)})


@router.delete('/cases/{case_id}/deposits/{allocation_id}', status_code=204)
def delete_allocation(case_id: int, allocation_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user['id'])
        allocation(con, case_id, allocation_id)
        con.execute('DELETE FROM allocations WHERE id=?', (allocation_id,))
        schedule(con, case_id, ['DEPOSIT_COMPARISON'], request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


@router.get('/cases/{case_id}/deposit-comparison', response_model=Envelope)
def deposit_comparison(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        row, ref = latest(con, case_id, 'depositComparison')
        return ok(snapshot_dto(row, ref['freshness']) if row else {'depositComparisonId': None,
            'freshness': ref['freshness'], 'status': 'INSUFFICIENT_INFORMATION', 'payslipNetPay': None,
            'linkedPaymentTotal': None, 'difference': None})


@router.get('/cases/{case_id}/deposit-comparisons', response_model=Envelope)
def deposit_comparisons(case_id: int, request: Request, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user['id'])
        return ok(snapshot_list(con, case_id, 'depositComparison', page, size))
