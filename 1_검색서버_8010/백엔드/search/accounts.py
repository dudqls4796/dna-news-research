"""회원·즐겨찾기 전용 라우터. 검색과 같은 DB 계정을 쓰되 이 라우터만 쓰기 연결을 연다."""

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
from threading import Lock
from time import monotonic
from typing import Annotated

from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'db'))
from load_bigkinds import DEFAULT_ENV, LoadError, connection_options  # noqa: E402

COOKIE = 'dice_session'
SESSION_SECONDS = 14 * 24 * 60 * 60
LOGIN_ERROR = '아이디 또는 비밀번호가 올바르지 않습니다.'


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'


_DUMMY_HASH = password_hash('dummy-password-never-used')


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split('$')
        if (algorithm, n, r, p) != ('scrypt', '16384', '8', '1'):
            return False
        actual = password_hash(password, bytes.fromhex(salt)).rsplit('$', 1)[1]
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class AttemptLimiter:
    # ponytail: 프로세스 메모리 한도. 재시작/여러 워커/분산 IP에 걸쳐 공유되지 않는다.
    # 프록시가 전달하는 IP의 신뢰 설정은 배포자가 관리한다. 원시 전달 헤더는 읽지 않는다.
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def check(self, key: str):
        now = monotonic()
        with self.lock:
            while self.entries and now - next(iter(self.entries.values()))[0] >= 60:
                self.entries.popitem(last=False)
            start, count = self.entries.get(key, (now, 0))
            if count >= 10 or (key not in self.entries and len(self.entries) >= 10000):
                raise HTTPException(429, '인증 시도가 너무 많습니다.', headers={'Retry-After': '60'})
            self.entries[key] = (start, count + 1)


attempts = AttemptLimiter()


def csrf(request: Request):
    # 이 헤더는 HTML form으로 보낼 수 없다. CORS를 허용하지 않아 교차 출처 preflight도 거절한다.
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-dice-csrf') != '1':
        raise HTTPException(403, 'X-DICE-CSRF: 1 헤더가 필요합니다.')


def limit_auth(request: Request):
    attempts.check(request.client.host if request.client else 'unknown')


class AccountRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Pydantic의 input 필드에 비밀번호가 반사되는 것을 차단한다.
                return JSONResponse({'error': '요청 형식 또는 입력값이 올바르지 않습니다.'}, status_code=422)
            except HTTPException as exc:
                return JSONResponse({'error': exc.detail}, status_code=exc.status_code, headers=exc.headers)
            except psycopg.errors.ForeignKeyViolation:
                return JSONResponse({'error': '참조 대상을 찾을 수 없습니다.'}, status_code=404)
            except psycopg.Error:
                return JSONResponse({'error': '회원 저장소를 사용할 수 없습니다.'}, status_code=503)

        return safe_handler


router = APIRouter(route_class=AccountRoute, dependencies=[Depends(csrf)])


@contextmanager
def database():
    # ponytail: 학원 DB에 역할 생성 권한이 없어 검색과 같은 DB 계정을 쓴다(2026-11-26 사용자 결정).
    # 검색 경로는 읽기 전용 연결을 유지하고, 이 연결만 쓰기 가능. 전용 역할이 생기면 BIGKINDS_DSN 대신 분리.
    try:
        options = connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV))))
    except LoadError:
        raise HTTPException(503, '회원 저장소가 설정되지 않았습니다.') from None
    # autocommit=False: route의 with 블록 종료 시 commit, 예외 시 rollback.
    with psycopg.connect(**options, row_factory=dict_row, autocommit=False, connect_timeout=5,
                         application_name='dice_accounts',
                         options='-c search_path=pg_catalog,public -c statement_timeout=5000 -c lock_timeout=3000') as conn:
        yield conn


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Credentials(Input):
    login_id: str = Field(min_length=4, max_length=20)
    password: str = Field(min_length=8, max_length=256)

    @field_validator('login_id')
    @classmethod
    def normalize_login(cls, value):
        # ASCII 검사 후 소문자 정규화하여 유니코드의 예상 밖 변환을 허용하지 않는다.
        if not re.fullmatch(r'[A-Za-z0-9_]{4,20}', value):
            raise ValueError('invalid login id')
        return value.lower()


class Withdrawal(Input):
    password: str = Field(min_length=8, max_length=256)


class Folder(Input):
    name: str = Field(min_length=1, max_length=100)
    query: str | None = Field(default=None, max_length=500)

    @field_validator('name')
    @classmethod
    def normalize_name(cls, value):
        value = ' '.join(value.split())
        if not value:
            raise ValueError('empty folder')
        return value


FavoriteId = Annotated[int, Field(gt=0, le=9223372036854775807)]
BatchIds = Annotated[list[FavoriteId], Field(min_length=1, max_length=500)]


class FavoriteTarget(Input):
    folder_id: FavoriteId | None = None
    saved_query: str | None = Field(default=None, max_length=500)


class Favorite(FavoriteTarget):
    article_id: FavoriteId


class FavoriteBatch(FavoriteTarget):
    article_ids: BatchIds


class FavoriteBatchDelete(Input):
    favorite_ids: BatchIds | None = None
    article_ids: BatchIds | None = None

    @model_validator(mode='after')
    def one_id_kind(self):
        if (self.favorite_ids is None) == (self.article_ids is None):
            raise ValueError('provide one list of IDs')
        return self


def current_user(conn, request):
    token = request.cookies.get(COOKIE, '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise HTTPException(401, '로그인이 필요합니다.')
    user = conn.execute('''SELECT u.id, u.login_id, u.password_hash, u.created_at, u.last_login_at
        FROM users u JOIN user_sessions s ON s.user_id = u.id
        WHERE s.token_hash = %s AND s.expires_at > %s FOR UPDATE OF u''',
        (token_hash(token), datetime.now(timezone.utc))).fetchone()
    if not user:
        raise HTTPException(401, '로그인이 필요합니다.')
    # 사용자 행 잠금으로 폴더 이동·삭제·탈퇴와 같은 사용자의 저장 요청을 직렬화한다.
    return user


# ponytail: 프로세스 메모리 캐시라 로그아웃·탈퇴는 이 프로세스에서만 즉시 지운다. 다른 워커에서
# 지운 세션은 최대 SESSION_CACHE_SECONDS 동안 통과할 수 있다. 여러 워커면 캐시를 끄거나 공유 저장소로 바꾼다.
SESSION_CACHE_SECONDS = 30
_session_cache: dict[str, float] = {}
_session_cache_lock = Lock()


def has_session(request) -> bool:
    """REQUIRE_LOGIN 검색 경로용 읽기 확인. 유효 세션이면 True, 없거나 만료면 False."""
    token = request.cookies.get(COOKIE, '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return False
    key, now = token_hash(token), monotonic()
    with _session_cache_lock:
        if _session_cache.get(key, 0) > now:
            return True
    with database() as conn:
        found = conn.execute('SELECT 1 FROM user_sessions WHERE token_hash = %s AND expires_at > %s',
                             (key, datetime.now(timezone.utc))).fetchone()
    if not found:
        return False
    with _session_cache_lock:
        if len(_session_cache) > 10000:
            _session_cache.clear()
        _session_cache[key] = now + SESSION_CACHE_SECONDS
    return True


def forget_sessions(token: str | None = None):
    with _session_cache_lock:
        if token is None:
            _session_cache.clear()
        else:
            _session_cache.pop(token_hash(token), None)


def public_user(user):
    return {key: user[key] for key in ('id', 'login_id', 'created_at', 'last_login_at')}


def secure_request(request):
    if request.url.scheme == 'https':
        return True
    # TLS 종료 터널: 명시한 신뢰 프록시의 전달 헤더만 사용한다(기본은 신뢰 안 함).
    trusted = {ip.strip() for ip in os.environ.get('AUTH_TRUSTED_PROXY_IPS', '').split(',') if ip.strip()}
    return bool(request.client and request.client.host in trusted
                and request.headers.get('x-forwarded-proto', '').lower() == 'https')


def clear_cookie(response, request):
    response.delete_cookie(COOKIE, path='/', httponly=True, samesite='lax', secure=secure_request(request))


@router.post('/api/auth/signup', status_code=201, dependencies=[Depends(limit_auth)])
def signup(body: Credentials):
    encoded = password_hash(body.password)
    try:
        with database() as conn:
            user = conn.execute('''INSERT INTO users (login_id, password_hash) VALUES (%s, %s)
                RETURNING id, login_id, created_at, last_login_at''', (body.login_id, encoded)).fetchone()
            conn.execute('''INSERT INTO favorite_folders (user_id, name, is_default)
                VALUES (%s, %s, true)''', (user['id'], '기본 폴더'))
        return {'user': user}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, '이미 사용 중인 아이디입니다.') from None


@router.post('/api/auth/login', dependencies=[Depends(limit_auth)])
def login(body: Credentials, request: Request, response: Response):
    with database() as conn:
        user = conn.execute('SELECT * FROM users WHERE login_id = %s FOR UPDATE', (body.login_id,)).fetchone()
        valid = verify_password(body.password, user['password_hash'] if user else _DUMMY_HASH)
        if not user or not valid:
            raise HTTPException(401, LOGIN_ERROR)
        now = datetime.now(timezone.utc)
        conn.execute('UPDATE users SET last_login_at = %s WHERE id = %s', (now, user['id']))
        # 현재 브라우저의 이전 세션도 폐기하여 로그인 시 세션을 교체한다.
        conn.execute('DELETE FROM user_sessions WHERE token_hash = %s OR (user_id = %s AND expires_at <= %s)',
                     (token_hash(request.cookies.get(COOKIE, '')), user['id'], now))
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=SESSION_SECONDS)
        conn.execute('INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)',
                     (token_hash(token), user['id'], expires))
        user['last_login_at'] = now
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, expires=expires, path='/',
                        httponly=True, samesite='lax', secure=secure_request(request))
    return {'user': public_user(user)}


@router.post('/api/auth/logout')
def logout(request: Request, response: Response):
    with database() as conn:
        conn.execute('DELETE FROM user_sessions WHERE token_hash = %s',
                     (token_hash(request.cookies.get(COOKIE, '')),))
    forget_sessions(request.cookies.get(COOKIE, ''))
    clear_cookie(response, request)
    return {'ok': True}


@router.get('/api/auth/me')
def me(request: Request):
    with database() as conn:
        return {'user': public_user(current_user(conn, request))}


@router.delete('/api/auth/me', dependencies=[Depends(limit_auth)])
def withdraw(body: Withdrawal, request: Request, response: Response):
    with database() as conn:
        user = current_user(conn, request)
        if not verify_password(body.password, user['password_hash']):
            raise HTTPException(401, '현재 비밀번호가 올바르지 않습니다.')
        conn.execute('DELETE FROM users WHERE id = %s', (user['id'],))
    forget_sessions()
    clear_cookie(response, request)
    return {'ok': True}


def owned_folder(conn, user_id, folder_id):
    if folder_id is None:
        row = conn.execute('SELECT * FROM favorite_folders WHERE user_id = %s AND is_default = true',
                           (user_id,)).fetchone()
    else:
        row = conn.execute('SELECT * FROM favorite_folders WHERE user_id = %s AND id = %s',
                           (user_id, folder_id)).fetchone()
    if not row:
        raise HTTPException(404, '폴더를 찾을 수 없습니다.')
    return row


@router.get('/api/favorites/folders')
def folders(request: Request):
    with database() as conn:
        user = current_user(conn, request)
        rows = conn.execute('''SELECT f.*, max(b.created_at) AS last_saved_at FROM favorite_folders f
            LEFT JOIN favorites b ON b.user_id = f.user_id AND b.folder_id = f.id
            WHERE f.user_id = %s GROUP BY f.id ORDER BY f.id''', (user['id'],)).fetchall()
        return {'folders': rows}


@router.post('/api/favorites/folders', status_code=201)
def create_folder(body: Folder, request: Request):
    try:
        with database() as conn:
            user = current_user(conn, request)
            row = conn.execute('''INSERT INTO favorite_folders (user_id, name, query) VALUES (%s, %s, %s)
                RETURNING *''', (user['id'], body.name, body.query)).fetchone()
        return {'folder': row}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, '같은 이름의 폴더가 있습니다.') from None


@router.patch('/api/favorites/folders/{folder_id}')
def rename_folder(body: Folder, request: Request,
                  folder_id: int = PathParam(gt=0, le=9223372036854775807)):
    try:
        with database() as conn:
            user = current_user(conn, request)
            owned_folder(conn, user['id'], folder_id)
            # 이름만 바꿀 때는 기존 검색어를 보존한다. query:null을 명시하면 제거한다.
            if 'query' in body.model_fields_set:
                conn.execute('UPDATE favorite_folders SET query = %s WHERE user_id = %s AND id = %s',
                             (body.query, user['id'], folder_id))
            row = conn.execute('''UPDATE favorite_folders SET name = %s WHERE user_id = %s AND id = %s
                RETURNING *''', (body.name, user['id'], folder_id)).fetchone()
        return {'folder': row}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, '같은 이름의 폴더가 있습니다.') from None


@router.delete('/api/favorites/folders/{folder_id}')
def delete_folder(request: Request, folder_id: int = PathParam(gt=0, le=9223372036854775807)):
    with database() as conn:
        user = current_user(conn, request)
        folder = owned_folder(conn, user['id'], folder_id)
        if folder['is_default']:
            raise HTTPException(409, '기본 폴더는 삭제할 수 없습니다.')
        target = owned_folder(conn, user['id'], None)
        conn.execute('''INSERT INTO favorites (user_id, article_id, folder_id, saved_query, created_at)
            SELECT user_id, article_id, %s, saved_query, created_at FROM favorites
            WHERE user_id = %s AND folder_id = %s
            ON CONFLICT (user_id, article_id, folder_id) DO NOTHING''', (target['id'], user['id'], folder_id))
        conn.execute('DELETE FROM favorite_folders WHERE user_id = %s AND id = %s', (user['id'], folder_id))
    return {'ok': True, 'moved_to_folder_id': target['id']}


FAVORITE_ROWS = '''SELECT f.*, a.title, a.url, a.published_date, s.publisher FROM favorites f
    JOIN articles a ON a.id = f.article_id LEFT JOIN article_sources s ON s.article_id = a.id'''


@router.get('/api/favorites')
def favorites(request: Request, folder_id: int | None = Query(default=None, gt=0, le=9223372036854775807),
              limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0, le=2147483647)):
    with database() as conn:
        user = current_user(conn, request)
        if folder_id is not None:
            owned_folder(conn, user['id'], folder_id)
        condition = 'f.user_id = %s'
        values = [user['id']]
        if folder_id is not None:
            condition += ' AND f.folder_id = %s'
            values.append(folder_id)
        total = conn.execute(f'SELECT count(*) AS total FROM favorites f WHERE {condition}', values).fetchone()['total']
        rows = conn.execute(f'''{FAVORITE_ROWS}
            WHERE {condition} ORDER BY f.created_at DESC, f.id DESC LIMIT %s OFFSET %s''',
            [*values, limit, offset]).fetchall()
        return {'favorites': rows, 'total': total, 'limit': limit, 'offset': offset}


@router.post('/api/favorites', status_code=201)
def add_favorite(body: Favorite, request: Request):
    with database() as conn:
        user = current_user(conn, request)
        folder = owned_folder(conn, user['id'], body.folder_id)
        if not conn.execute('SELECT id FROM articles WHERE id = %s', (body.article_id,)).fetchone():
            raise HTTPException(404, '기사를 찾을 수 없습니다.')
        row = conn.execute('''INSERT INTO favorites (user_id, article_id, folder_id, saved_query)
            VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, article_id, folder_id)
            DO UPDATE SET saved_query = EXCLUDED.saved_query RETURNING *''',
            (user['id'], body.article_id, folder['id'], body.saved_query)).fetchone()
        return {'favorite': row}


@router.delete('/api/favorites')
def remove_favorite(request: Request, favorite_id: int = Query(gt=0, le=9223372036854775807)):
    with database() as conn:
        user = current_user(conn, request)
        row = conn.execute('DELETE FROM favorites WHERE user_id = %s AND id = %s RETURNING id',
                           (user['id'], favorite_id)).fetchone()
        if not row:
            raise HTTPException(404, '즐겨찾기를 찾을 수 없습니다.')
    return {'ok': True}


@router.post('/api/favorites/batch', status_code=201)
def add_favorites(body: FavoriteBatch, request: Request):
    article_ids = list(dict.fromkeys(body.article_ids))
    placeholders = ', '.join(['%s'] * len(article_ids))
    with database() as conn:
        user_id = current_user(conn, request)['id']
        folder_id = owned_folder(conn, user_id, body.folder_id)['id']
        conn.execute(f'''INSERT INTO favorites (user_id, article_id, folder_id, saved_query)
            SELECT %s, id, %s, %s FROM articles WHERE id IN ({placeholders})
            ON CONFLICT (user_id, article_id, folder_id) DO NOTHING''',
            [user_id, folder_id, body.saved_query, *article_ids])
        rows = conn.execute(f'''{FAVORITE_ROWS}
            WHERE f.user_id = %s AND f.folder_id = %s AND f.article_id IN ({placeholders})
            ORDER BY f.created_at DESC, f.id DESC''', [user_id, folder_id, *article_ids]).fetchall()
        if len(rows) != len(article_ids):
            raise HTTPException(404, '기사를 찾을 수 없습니다.')
    return {'favorites': rows}


@router.post('/api/favorites/batch-delete')
def remove_favorites(body: FavoriteBatchDelete, request: Request):
    if body.favorite_ids is not None:
        column, ids = 'id', body.favorite_ids
    else:
        column, ids = 'article_id', body.article_ids
    ids = list(dict.fromkeys(ids))
    placeholders = ', '.join(['%s'] * len(ids))
    with database() as conn:
        user_id = current_user(conn, request)['id']
        rows = conn.execute(f'''DELETE FROM favorites WHERE user_id = %s AND {column} IN ({placeholders})
            RETURNING id, article_id''', [user_id, *ids]).fetchall()
    return {'deleted': rows}
