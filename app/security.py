import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import now
from app.errors import ApiError


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200000)
    return salt + ':' + result.hex()


def verify_password(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def issue_session(con, user_id):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    current = datetime.now(timezone.utc)
    con.execute('INSERT INTO sessions(user_id,access_hash,refresh_hash,access_expires,refresh_expires) VALUES(?,?,?,?,?)',
                (user_id, digest(access), digest(refresh), (current + timedelta(minutes=30)).isoformat(),
                 (current + timedelta(days=14)).isoformat()))
    return access, refresh


bearer = HTTPBearer(auto_error=False)


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None:
        raise ApiError(401, 'AUTH_REQUIRED')
    with request.app.state.db.connect() as con:
        user = con.execute('''SELECT u.*, s.id AS session_id, s.access_expires FROM users u
            JOIN sessions s ON u.id=s.user_id WHERE s.access_hash=? AND u.deleted_at IS NULL''',
                           (digest(credentials.credentials),)).fetchone()
    if user is None:
        raise ApiError(401, 'AUTH_REQUIRED')
    if user['access_expires'] <= now():
        raise ApiError(401, 'TOKEN_EXPIRED')
    return dict(user)


def workplace(con, workplace_id, user_id):
    row = con.execute('SELECT * FROM workplaces WHERE id=? AND user_id=? AND deleted_at IS NULL',
                      (workplace_id, user_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def case(con, case_id, user_id):
    row = con.execute('''SELECT c.* FROM cases c JOIN workplaces w ON w.id=c.workplace_id
        WHERE c.id=? AND w.user_id=? AND c.deleted_at IS NULL AND w.deleted_at IS NULL''',
                      (case_id, user_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def document(con, workplace_id, document_id, user_id):
    workplace(con, workplace_id, user_id)
    row = con.execute('SELECT * FROM documents WHERE id=? AND workplace_id=? AND deleted_at IS NULL',
                      (document_id, workplace_id)).fetchone()
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return row


def case_document(con, case_id, document_id, user_id):
    c = case(con, case_id, user_id)
    doc = document(con, c['workplace_id'], document_id, user_id)
    link = con.execute('SELECT * FROM document_links WHERE case_id=? AND document_id=?',
                       (case_id, document_id)).fetchone()
    if link is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND')
    return doc, link


def check_version(row, expected):
    if expected is not None and expected != row['input_version']:
        raise ApiError(409, 'INPUT_VERSION_CONFLICT', details={
            'expectedInputVersion': expected, 'currentInputVersion': row['input_version']})
