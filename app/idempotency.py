"""Cache supported JSON mutations inside the same transaction as their domain writes."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import Header

from app.db import dump, load, now
from app.errors import ApiError, ok


def idempotency_header(idempotency_key: str | None = Header(default=None, alias='Idempotency-Key', min_length=1, max_length=128)):
    """Document the optional header on endpoints whose transaction stores the response."""
    return idempotency_key


def identity(request, body):
    key = request.headers.get('idempotency-key')
    if key is None:
        return None
    if not 1 <= len(key) <= 128 or not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ApiError(422, 'INVALID_IDEMPOTENCY_KEY')
    payload = json.dumps(body.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(payload.encode()).hexdigest()


def replay(con, request, user_id, body):
    ident = identity(request, body)
    if ident is None:
        return None
    key_hash, fingerprint = ident
    con.execute('DELETE FROM idempotency_responses WHERE expires_at<=?', (now(),))
    row = con.execute('SELECT * FROM idempotency_responses WHERE user_id=? AND path=? AND key_hash=?',
                      (user_id, request.url.path, key_hash)).fetchone()
    if row is None:
        return None
    if row['fingerprint'] != fingerprint:
        raise ApiError(409, 'IDEMPOTENCY_KEY_REUSED', '같은 Idempotency-Key를 다른 요청 내용에 사용할 수 없습니다.')
    response = ok(load(row['data_json']), row['status_code'])
    response.headers['Idempotency-Replayed'] = 'true'
    return response


def remember(con, request, user_id, body, data, status=202):
    ident = identity(request, body)
    if ident is not None:
        key_hash, fingerprint = ident
        expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        con.execute('INSERT INTO idempotency_responses(user_id,path,key_hash,fingerprint,data_json,status_code,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)',
                    (user_id, request.url.path, key_hash, fingerprint, dump(data), status, now(), expiry))
    return ok(data, status)
