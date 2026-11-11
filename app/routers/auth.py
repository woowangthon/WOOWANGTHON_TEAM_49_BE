import sqlite3
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response

from app.db import dump, now
from app.errors import ApiError, ok
from app.outputs import API, LoginDTO, UserDTO
from app.schemas import DeleteAccount, ForgotPassword, Login, ResetPassword, Signup
from app.security import current_user, digest, issue_session, password_hash, verify_password

router = APIRouter(tags=['Auth'])


def user_dto(user):
    return {'userId': user['id'], 'email': user['email'], 'name': user['name']}


def session_response(con, user, settings):
    access, refresh = issue_session(con, user['id'])
    response = ok({'accessToken': access, 'tokenType': 'Bearer', 'expiresIn': 1800, 'user': user_dto(user)})
    response.set_cookie('refresh_token', refresh, max_age=14*86400, httponly=True,
                        secure=settings.cookie_secure, samesite='lax', path='/api/v1/auth')
    response.delete_cookie('deletion_receipt', path='/api/v1/users/me/deletion')
    return response


@router.post('/auth/signup', status_code=201)
def signup(body: Signup, request: Request):
    hashed = password_hash(body.password)
    try:
        with request.app.state.db.connect(write=True) as con:
            user_id = con.execute('INSERT INTO users(email,name,password_hash) VALUES(?,?,?)',
                                  (body.email.lower(), body.name, hashed)).lastrowid
    except sqlite3.IntegrityError:
        raise ApiError(409, 'EMAIL_ALREADY_EXISTS')
    return ok({'userId': user_id}, 201)


@router.post('/auth/login', response_model=API[LoginDTO])
def login(body: Login, request: Request):
    with request.app.state.db.connect(write=True) as con:
        user = con.execute('SELECT * FROM users WHERE email=? AND deleted_at IS NULL', (body.email.lower(),)).fetchone()
        if user is None or not verify_password(body.password, user['password_hash']):
            raise ApiError(401, 'INVALID_CREDENTIALS')
        return session_response(con, user, request.app.state.settings)


@router.post('/auth/refresh', response_model=API[LoginDTO])
def refresh(request: Request):
    token = request.cookies.get('refresh_token')
    with request.app.state.db.connect(write=True) as con:
        user = con.execute('''SELECT u.*,s.id AS session_id FROM users u JOIN sessions s ON s.user_id=u.id
            WHERE s.refresh_hash=? AND s.refresh_expires>? AND u.deleted_at IS NULL''', (digest(token or ''), now())).fetchone()
        if user is None:
            raise ApiError(401, 'AUTH_REQUIRED')
        con.execute('DELETE FROM sessions WHERE id=?', (user['session_id'],))
        return session_response(con, user, request.app.state.settings)


@router.post('/auth/logout', status_code=204)
def logout(request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        con.execute('DELETE FROM sessions WHERE id=?', (user['session_id'],))
    response = Response(status_code=204)
    response.delete_cookie('refresh_token', path='/api/v1/auth')
    return response


@router.post('/auth/forgot-password', status_code=202)
def forgot_password(body: ForgotPassword, request: Request):
    settings = request.app.state.settings
    if settings.password_reset_delivery != 'file':
        raise ApiError(503, 'PASSWORD_RESET_NOT_CONFIGURED')
    with request.app.state.db.connect(write=True) as con:
        user = con.execute('SELECT * FROM users WHERE email=? AND deleted_at IS NULL', (body.email.lower(),)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            con.execute('UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL', (now(), user['id']))
            con.execute('INSERT INTO password_resets(user_id,token_hash,expires_at) VALUES(?,?,?)', (user['id'], digest(token), expiry))
            outbox = settings.data_dir / 'outbox'
            outbox.mkdir(mode=0o700, exist_ok=True)
            path = outbox / (uuid.uuid4().hex + '.json')
            message = {'deliveryMode': 'LOCAL_OUTBOX_ONLY', 'to': user['email'], 'expiresAt': expiry,
                       'resetUrl': settings.frontend_base_url.rstrip('/') + '/reset-password?' + urlencode({'token': token}),
                       'notice': '개발용 파일입니다. 실제 이메일은 발송하지 않았습니다.'}
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                stream.write(dump(message))
    # The API never reveals whether the email exists or exposes the reset token.
    return ok({'deliveryMode': 'LOCAL_OUTBOX_ONLY', 'expiresIn': 1800}, 202,
              '등록된 계정이면 개발용 로컬 outbox에 재설정 안내가 생성됩니다. 실제 이메일은 발송하지 않습니다.')


@router.post('/auth/reset-password', status_code=204)
def reset_password(body: ResetPassword, request: Request):
    hashed_password = password_hash(body.newPassword)
    with request.app.state.db.connect(write=True) as con:
        reset = con.execute('''SELECT r.* FROM password_resets r JOIN users u ON u.id=r.user_id
            WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND u.deleted_at IS NULL''', (digest(body.token), now())).fetchone()
        if reset is None:
            raise ApiError(400, 'INVALID_RESET_TOKEN')
        con.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed_password, reset['user_id']))
        con.execute('UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL', (now(), reset['user_id']))
        con.execute('DELETE FROM sessions WHERE user_id=?', (reset['user_id'],))
    return Response(status_code=204)


@router.get('/users/me', response_model=API[UserDTO])
def me(user=Depends(current_user)):
    return ok(user_dto(user))


@router.delete('/users/me', status_code=202)
def delete_account(body: DeleteAccount, request: Request, user=Depends(current_user)):
    timestamp = now()
    receipt = secrets.token_urlsafe(32)
    with request.app.state.db.connect(write=True) as con:
        con.execute('UPDATE users SET deleted_at=? WHERE id=?', (timestamp, user['id']))
        con.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        con.execute('UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL', (timestamp, user['id']))
        con.execute('UPDATE workplaces SET deleted_at=? WHERE user_id=?', (timestamp, user['id']))
        con.execute("UPDATE jobs SET status='SUPERSEDED',completed_at=? WHERE case_id IN (SELECT c.id FROM cases c JOIN workplaces w ON w.id=c.workplace_id WHERE w.user_id=?) AND status IN ('PENDING','RUNNING')", (timestamp, user['id']))
        rid = con.execute('INSERT INTO deletion_receipts(user_id,token_hash,access_revoked_at,expires_at) VALUES(?,?,?,?)',
                          (user['id'], digest(receipt), timestamp, (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())).lastrowid
    response = ok({'deletionRequestId': rid, 'status': 'ACCESS_REVOKED', 'accessRevokedAt': timestamp,
                   'normalStoragePurgeDueAt': None, 'purgeImplemented': False}, 202)
    response.delete_cookie('refresh_token', path='/api/v1/auth')
    response.set_cookie('deletion_receipt', receipt, max_age=30*86400, httponly=True,
                        secure=request.app.state.settings.cookie_secure, samesite='lax', path='/api/v1/users/me/deletion')
    return response


@router.get('/users/me/deletion')
def deletion_status(request: Request):
    receipt = request.cookies.get('deletion_receipt', '')
    with request.app.state.db.connect() as con:
        row = con.execute('SELECT * FROM deletion_receipts WHERE token_hash=? AND expires_at>?', (digest(receipt), now())).fetchone()
        if row is None:
            raise ApiError(401, 'DELETION_RECEIPT_REQUIRED')
        return ok({'deletionRequestId': row['id'], 'status': 'ACCESS_REVOKED', 'accessRevokedAt': row['access_revoked_at'],
                   'normalStoragePurgeDueAt': None, 'purgeImplemented': False})
