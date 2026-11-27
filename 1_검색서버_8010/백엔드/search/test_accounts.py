"""소켓/실제 DB 없이 HTTP 계약과 SQL 동작을 검사한다.

SQLite 메모리 어댑터는 migration DDL의 타입·정규식·잠금 문법만 변환한다.
PostgreSQL 권한/동시 행 잠금 검증은 승인 후 별도 환경에서 수행해야 한다.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import psycopg
import pytest

from search import accounts, app

MIGRATION = Path(__file__).resolve().parents[1] / 'db/migrations/003_users_favorites.sql'
CSRF = {'X-DICE-CSRF': '1'}
PASSWORD = 'test-password-123'


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class MemoryDB:
    def __init__(self):
        self.raw = sqlite3.connect(':memory:', check_same_thread=False)
        self.raw.row_factory = sqlite3.Row
        self.raw.create_function('now', 0, lambda: datetime.now(timezone.utc).isoformat())
        self.raw.create_function('regexp', 2, lambda pattern, value: bool(re.fullmatch(pattern, value)))
        self.raw.execute('PRAGMA foreign_keys=ON')
        self.raw.executescript('''CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, url TEXT, published_date TEXT);
            CREATE TABLE article_sources (article_id INTEGER PRIMARY KEY, publisher TEXT);
            INSERT INTO articles VALUES (1, '테스트 제목', 'https://example.test/1', '2026-11-26');
            INSERT INTO articles VALUES (2, '두 번째 제목', NULL, '2026-11-25');
            INSERT INTO article_sources VALUES (1, '테스트일보');''')
        ddl = MIGRATION.read_text(encoding='utf-8')
        ddl = ddl.replace("SET LOCAL lock_timeout = '5s';", '')
        ddl = ddl.replace('BIGSERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')
        ddl = ddl.replace('DEFAULT now()', 'DEFAULT (now())').replace(' ~ ', ' REGEXP ')
        self.raw.executescript(ddl)
        self.queries = []
        self.transactions = 0

    def execute(self, sql, params=()):
        self.queries.append(sql)
        sql = re.sub(r' FOR UPDATE(?: OF u)?', '', sql).replace('%s', '?')
        params = [value.isoformat() if isinstance(value, datetime) else value for value in params]
        try:
            return Cursor(self.raw.execute(sql, params))
        except sqlite3.IntegrityError as exc:
            if 'UNIQUE' in str(exc):
                raise psycopg.errors.UniqueViolation('mock unique') from None
            if 'FOREIGN KEY' in str(exc):
                raise psycopg.errors.ForeignKeyViolation('mock foreign key') from None
            raise

    @contextmanager
    def transaction(self):
        self.transactions += 1
        with self.raw:
            yield self


@pytest.fixture
def http(monkeypatch):
    db = MemoryDB()
    monkeypatch.setattr(accounts, 'database', db.transaction)
    monkeypatch.setattr(accounts, 'attempts', accounts.AttemptLimiter())
    monkeypatch.setattr(app, '_requests', {})
    monkeypatch.delenv('SEARCH_PASSWORD', raising=False)
    monkeypatch.delenv('AUTH_TRUSTED_PROXY_IPS', raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('실제 PostgreSQL 연결 금지')
    monkeypatch.setattr(psycopg, 'connect', forbidden)
    # lifespan을 시작하지 않아 기존 모델/DB warmup도 실행하지 않는다.
    client = TestClient(app.app, headers=CSRF)
    yield client, db
    client.close()
    db.raw.close()


def signup(client, login_id='tester'):
    return client.post('/api/auth/signup', json={'login_id': login_id, 'password': PASSWORD})


def login(client, login_id='tester', password=PASSWORD):
    return client.post('/api/auth/login', json={'login_id': login_id, 'password': password})


def registered(client, login_id='tester'):
    assert signup(client, login_id).status_code == 201
    assert login(client, login_id).status_code == 200


def create_folder(client, name='분석', query='검색어'):
    response = client.post('/api/favorites/folders', json={'name': name, 'query': query})
    assert response.status_code == 201, response.text
    return response.json()['folder']['id']


def add(client, article_id=1, folder_id=None):
    return client.post('/api/favorites', json={'article_id': article_id, 'folder_id': folder_id, 'saved_query': '검색어'})


def test_signup_normalizes_duplicate_and_password_storage(http):
    client, db = http
    response = signup(client, 'TeSTer')
    assert response.status_code == 201
    assert response.json()['user']['login_id'] == 'tester'
    assert 'password' not in response.text
    assert accounts.COOKIE not in client.cookies  # 가입 후 명시적으로 로그인
    assert signup(client, 'tester').status_code == 409
    user = db.execute('SELECT * FROM users').fetchone()
    assert user['password_hash'] != PASSWORD
    assert accounts.verify_password(PASSWORD, user['password_hash'])
    assert db.execute('SELECT count(*) AS n FROM favorite_folders').fetchone()['n'] == 1


@pytest.mark.parametrize('body', [
    {'login_id': 'abc', 'password': PASSWORD}, {'login_id': '한글아이디', 'password': PASSWORD},
    {'login_id': 'a' * 21, 'password': PASSWORD}, {'login_id': 'test-er', 'password': PASSWORD},
    {'login_id': 'tester', 'password': 'short'}, {'login_id': 'tester', 'password': 'x' * 257},
    {'login_id': 'tester', 'password': PASSWORD, 'email': 'x@example.test'},
])
def test_credentials_validation_no_password_echo(http, body):
    client, db = http
    response = client.post('/api/auth/signup', json=body)
    assert response.status_code == 422
    assert body['password'] not in response.text
    assert db.execute('SELECT count(*) AS n FROM users').fetchone()['n'] == 0


def test_password_hash_unique_salt_and_verification():
    first, second = accounts.password_hash(PASSWORD), accounts.password_hash(PASSWORD)
    assert first != second
    assert accounts.verify_password(PASSWORD, first)
    assert not accounts.verify_password('wrong-password', first)
    assert not accounts.verify_password(PASSWORD, 'broken')


def test_login_failure_same_response_and_dummy_scrypt(http, monkeypatch):
    client, db = http
    signup(client)
    original = accounts.verify_password
    calls = []
    def spy(password, encoded):
        calls.append(encoded)
        return original(password, encoded)
    monkeypatch.setattr(accounts, 'verify_password', spy)
    known = login(client, password='wrong-password')
    absent = login(client, 'missing', 'wrong-password')
    assert known.status_code == absent.status_code == 401
    assert known.json() == absent.json() == {'error': accounts.LOGIN_ERROR}
    assert len(calls) == 2 and calls[1] == accounts._DUMMY_HASH
    assert db.execute('SELECT count(*) AS n FROM user_sessions').fetchone()['n'] == 0


def test_session_cookie_hash_rotation_logout_and_expiry(http):
    client, db = http
    signup(client)
    response = login(client)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Max-Age=1209600' in cookie
    assert 'Secure' not in cookie and 'Domain=' not in cookie and 'Path=/' in cookie
    token = client.cookies.get(accounts.COOKIE)
    row = db.execute('SELECT * FROM user_sessions').fetchone()
    assert row['token_hash'] == accounts.token_hash(token) and row['token_hash'] != token
    assert client.get('/api/auth/me').json()['user']['last_login_at']
    assert 'password_hash' not in client.get('/api/auth/me').text
    login(client)
    assert client.cookies.get(accounts.COOKIE) != token
    assert db.execute('SELECT count(*) AS n FROM user_sessions').fetchone()['n'] == 1
    assert client.post('/api/auth/logout').json() == {'ok': True}
    assert not client.cookies.get(accounts.COOKIE)
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/logout').status_code == 200
    login(client)
    now = datetime.now(timezone.utc)
    db.execute('UPDATE user_sessions SET created_at = %s, expires_at = %s',
               (now - timedelta(days=15), now - timedelta(seconds=1)))
    db.raw.commit()
    assert client.get('/api/auth/me').status_code == 401
    assert add(client).status_code == 401


def test_secure_cookie_https_and_explicit_proxy_trust(http, monkeypatch):
    client, _ = http
    signup(client)
    client.base_url = 'https://testserver'
    assert 'Secure' in login(client).headers['set-cookie']
    assert 'Secure' in client.post('/api/auth/logout').headers['set-cookie']
    client.base_url = 'http://testserver'
    client.headers['X-Forwarded-Proto'] = 'https'
    assert 'Secure' not in login(client).headers['set-cookie']
    monkeypatch.setenv('AUTH_TRUSTED_PROXY_IPS', 'testclient')
    assert 'Secure' in login(client).headers['set-cookie']


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/api/auth/signup', {'login_id': 'tester', 'password': PASSWORD}),
    ('POST', '/api/auth/login', {'login_id': 'tester', 'password': PASSWORD}),
    ('POST', '/api/auth/logout', None), ('DELETE', '/api/auth/me', {'password': PASSWORD}),
    ('POST', '/api/favorites', {'article_id': 1}), ('DELETE', '/api/favorites?favorite_id=1', None),
    ('POST', '/api/favorites/batch', {'article_ids': [1]}),
    ('POST', '/api/favorites/batch-delete', {'article_ids': [1]}),
    ('POST', '/api/favorites/folders', {'name': 'folder'}),
    ('PATCH', '/api/favorites/folders/1', {'name': 'folder'}),
    ('DELETE', '/api/favorites/folders/1', None),
])
def test_all_mutations_require_csrf_before_database(http, method, path, body):
    client, db = http
    client.headers.pop('x-dice-csrf')
    assert client.request(method, path, json=body, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert db.transactions == 0


def test_no_cross_origin_preflight(http):
    client, _ = http
    response = client.options('/api/auth/login', headers={'Origin': 'https://evil.test',
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'x-dice-csrf'})
    assert 'access-control-allow-origin' not in response.headers
    assert response.status_code == 405


def test_auth_rate_limit(http):
    client, _ = http
    for _ in range(10):
        assert login(client, 'missing').status_code == 401
    response = login(client, 'missing')
    assert response.status_code == 429 and response.headers['retry-after'] == '60'


def test_rate_limit_window_and_bounded_memory(monkeypatch):
    limiter = accounts.AttemptLimiter()
    clock = [1.0]
    monkeypatch.setattr(accounts, 'monotonic', lambda: clock[0])
    for _ in range(10):
        limiter.check('ip')
    with pytest.raises(accounts.HTTPException):
        limiter.check('ip')
    clock[0] = 62.0
    limiter.check('ip')
    assert limiter.entries['ip'][1] == 1


def test_favorites_metadata_dedup_folders_and_delete(http):
    client, db = http
    registered(client)
    folder = create_folder(client, '  경제   뉴스  ')
    duplicate = client.post('/api/favorites/folders', json={'name': '경제 뉴스'})
    assert duplicate.status_code == 409
    first = add(client, folder_id=folder).json()['favorite']
    assert add(client, folder_id=folder).json()['favorite']['id'] == first['id']
    assert add(client, 999).status_code == 404
    response = client.get('/api/favorites', params={'folder_id': folder, 'limit': 1})
    assert response.status_code == 200
    row = response.json()['favorites'][0]
    assert row['article_id'] == 1 and row['title'] == '테스트 제목' and row['publisher'] == '테스트일보'
    assert row['saved_query'] == '검색어' and 'body' not in row
    updated = client.patch(f'/api/favorites/folders/{folder}', json={'name': '새 이름'}).json()['folder']
    assert updated['name'] == '새 이름' and updated['query'] == '검색어'
    rows = client.get('/api/favorites/folders').json()['folders']
    assert next(row for row in rows if row['id'] == folder)['last_saved_at']
    assert client.delete('/api/favorites', params={'favorite_id': first['id']}).status_code == 200
    assert client.delete('/api/favorites', params={'favorite_id': first['id']}).status_code == 404
    assert client.get('/api/favorites').json()['total'] == 0


def test_folder_delete_moves_and_merges_into_default(http):
    client, db = http
    registered(client)
    default = client.get('/api/favorites/folders').json()['folders'][0]['id']
    folder = create_folder(client)
    existing = add(client).json()['favorite']
    add(client, folder_id=folder)
    add(client, 2, folder)
    assert client.delete(f'/api/favorites/folders/{default}').status_code == 409
    response = client.delete(f'/api/favorites/folders/{folder}')
    assert response.json()['moved_to_folder_id'] == default
    rows = client.get('/api/favorites').json()['favorites']
    assert {row['article_id'] for row in rows} == {1, 2}
    assert all(row['folder_id'] == default for row in rows)
    assert next(row for row in rows if row['article_id'] == 1)['id'] == existing['id']
    assert db.execute('SELECT count(*) AS n FROM favorite_folders').fetchone()['n'] == 1


def test_other_user_isolation_and_composite_foreign_key(http):
    client, db = http
    registered(client, 'user_one')
    folder = create_folder(client)
    saved = add(client, folder_id=folder).json()['favorite']
    registered(client, 'user_two')
    second_id = client.get('/api/auth/me').json()['user']['id']
    assert client.get('/api/favorites').json()['favorites'] == []
    assert client.get('/api/favorites', params={'folder_id': folder}).status_code == 404
    assert add(client, folder_id=folder).status_code == 404
    assert client.patch(f'/api/favorites/folders/{folder}', json={'name': 'hijack'}).status_code == 404
    assert client.delete(f'/api/favorites/folders/{folder}').status_code == 404
    assert client.delete('/api/favorites', params={'favorite_id': saved['id']}).status_code == 404
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute('INSERT INTO favorites (user_id, article_id, folder_id) VALUES (%s, %s, %s)', (second_id, 1, folder))
    assert db.execute('SELECT count(*) AS n FROM favorites').fetchone()['n'] == 1


def test_withdraw_password_recheck_cascades_all_sessions_and_preserves_other_user(http):
    client, db = http
    registered(client, 'user_one')
    first_user = client.get('/api/auth/me').json()['user']['id']
    add(client)
    first_token = client.cookies.get(accounts.COOKIE)
    client.cookies.clear()
    login(client, 'user_one')
    assert db.execute('SELECT count(*) AS n FROM user_sessions').fetchone()['n'] == 2
    signup(client, 'user_two')
    assert client.request('DELETE', '/api/auth/me', json={'password': 'wrong-password'}).status_code == 401
    response = client.request('DELETE', '/api/auth/me', json={'password': PASSWORD})
    assert response.status_code == 200 and not client.cookies.get(accounts.COOKIE)
    for table in ('favorites', 'favorite_folders', 'user_sessions'):
        assert db.execute(f'SELECT count(*) AS n FROM {table} WHERE user_id = %s', (first_user,)).fetchone()['n'] == 0
    assert db.execute('SELECT count(*) AS n FROM users').fetchone()['n'] == 1
    assert db.execute('SELECT count(*) AS n FROM articles').fetchone()['n'] == 2
    client.cookies.set(accounts.COOKIE, first_token)
    assert client.get('/api/auth/me').status_code == 401


def test_article_delete_cascades_only_bookmark(http):
    client, db = http
    registered(client)
    add(client)
    db.execute('DELETE FROM articles WHERE id = 1')
    db.raw.commit()
    assert client.get('/api/favorites').json()['total'] == 0
    assert client.get('/api/auth/me').status_code == 200


def test_signup_failure_rolls_back_user(http, monkeypatch):
    client, db = http
    execute = db.execute
    def fail_folder(sql, params=()):
        if 'INSERT INTO favorite_folders' in sql:
            raise psycopg.OperationalError('비밀 접속정보가 있는 가상 내부 오류')
        return execute(sql, params)
    monkeypatch.setattr(db, 'execute', fail_folder)
    response = signup(client)
    assert response.status_code == 503
    assert '비밀' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert execute('SELECT count(*) AS n FROM users').fetchone()['n'] == 0


def test_folder_move_failure_rolls_back_insert(http, monkeypatch):
    client, db = http
    registered(client)
    folder = create_folder(client)
    favorite = add(client, folder_id=folder).json()['favorite']
    execute = db.execute
    def fail_delete(sql, params=()):
        if sql.startswith('DELETE FROM favorite_folders'):
            raise psycopg.OperationalError('mock failure')
        return execute(sql, params)
    monkeypatch.setattr(db, 'execute', fail_delete)
    assert client.delete(f'/api/favorites/folders/{folder}').status_code == 503
    rows = client.get('/api/favorites').json()['favorites']
    assert len(rows) == 1 and rows[0]['id'] == favorite['id'] and rows[0]['folder_id'] == folder


@pytest.mark.parametrize('folder_id', ['0', '-1', '9223372036854775808', 'abc'])
def test_folder_path_id_validation(http, folder_id):
    client, db = http
    assert client.delete(f'/api/favorites/folders/{folder_id}').status_code == 422
    assert not db.queries


def test_metadata_only_rejects_body_and_user_id(http):
    client, _ = http
    registered(client)
    for extra in ({'body': '본문'}, {'user_id': 99}, {'title': '클라이언트 제목'}, {'article_id': True}):
        response = client.post('/api/favorites', json={'article_id': 1, **extra})
        assert response.status_code == 422
    assert client.get('/api/favorites').json()['total'] == 0


@pytest.mark.parametrize('path,body', [
    ('batch', {'article_ids': [1]}),
    ('batch-delete', {'favorite_ids': [1]}),
])
def test_favorite_batches_require_session(http, path, body):
    client, db = http
    assert client.post(f'/api/favorites/{path}', json=body).status_code == 401
    assert not db.queries


def test_favorite_batch_deduplicates_and_returns_list_rows(http, monkeypatch):
    client, db = http
    monkeypatch.setenv('REQUIRE_LOGIN', '1')
    registered(client)
    folder = create_folder(client)
    existing = add(client, folder_id=folder).json()['favorite']
    body = {'article_ids': [1, 2, 1, 2], 'folder_id': folder, 'saved_query': '새 검색어'}
    for _ in range(21):  # 검색 요청 한도를 넘겨도 회원 API는 처리한다.
        before = db.transactions
        response = client.post('/api/favorites/batch', json=body)
        assert response.status_code == 201, response.text
        assert db.transactions == before + 1
    rows = response.json()['favorites']
    assert rows == client.get('/api/favorites').json()['favorites']
    assert len(rows) == 2
    by_article = {row['article_id']: row for row in rows}
    assert {key: by_article[1][key] for key in existing} == existing
    assert by_article[1]['publisher'] == '테스트일보'
    assert by_article[2]['saved_query'] == '새 검색어' and by_article[2]['publisher'] is None
    assert app._requests == {}


def test_favorite_batch_rolls_back_when_article_is_missing(http):
    client, _ = http
    registered(client)
    existing = add(client).json()['favorite']
    response = client.post('/api/favorites/batch', json={'article_ids': [1, 2, 404]})
    assert response.status_code == 404
    rows = client.get('/api/favorites').json()['favorites']
    assert [row['id'] for row in rows] == [existing['id']]


def test_favorite_batches_isolate_users_and_delete_all_matching_folders(http):
    client, db = http
    registered(client, 'first_user')
    other_folder = create_folder(client)
    other = add(client, folder_id=other_folder).json()['favorite']
    registered(client, 'second_user')
    folder = create_folder(client)
    assert client.post('/api/favorites/batch', json={
        'article_ids': [2], 'folder_id': other_folder,
    }).status_code == 404
    default = add(client).json()['favorite']
    second = client.post('/api/favorites/batch', json={
        'article_ids': [1, 2], 'folder_id': folder,
    }).json()['favorites']
    by_article = {row['article_id']: row['id'] for row in second}
    before = db.transactions
    response = client.post('/api/favorites/batch-delete', json={
        'favorite_ids': [other['id'], by_article[2], by_article[2]],
    })
    assert response.status_code == 200
    assert response.json() == {'deleted': [{'id': by_article[2], 'article_id': 2}]}
    assert db.transactions == before + 1
    response = client.post('/api/favorites/batch-delete', json={'article_ids': [1, 1, 404]})
    assert response.status_code == 200
    assert {row['id'] for row in response.json()['deleted']} == {default['id'], by_article[1]}
    assert client.post('/api/favorites/batch-delete', json={'article_ids': [1]}).json() == {'deleted': []}
    assert client.get('/api/favorites').json()['favorites'] == []
    assert login(client, 'first_user').status_code == 200
    assert [row['id'] for row in client.get('/api/favorites').json()['favorites']] == [other['id']]


@pytest.mark.parametrize('path,body', [
    ('batch', {'article_ids': []}),
    ('batch', {'article_ids': [1] * 501}),
    ('batch', {'article_ids': [True]}),
    ('batch', {'article_ids': [1], 'folder_id': 0}),
    ('batch', {'article_ids': [1], 'saved_query': '가' * 501}),
    ('batch', {'article_ids': [1], 'body': '기사 본문'}),
    ('batch-delete', {}),
    ('batch-delete', {'favorite_ids': [1], 'article_ids': [1]}),
    ('batch-delete', {'favorite_ids': ['1']}),
    ('batch-delete', {'article_ids': [9223372036854775808]}),
    ('batch-delete', {'favorite_ids': [1] * 501}),
])
def test_favorite_batch_validation(http, path, body):
    client, db = http
    assert client.post(f'/api/favorites/{path}', json=body).status_code == 422
    assert db.transactions == 0


def test_missing_auth_config_search_still_works(monkeypatch, tmp_path):
    monkeypatch.delenv('BIGKINDS_DSN', raising=False)
    monkeypatch.setenv('BIGKINDS_ENV_FILE', str(tmp_path / 'missing.env'))
    monkeypatch.delenv('SEARCH_PASSWORD', raising=False)
    monkeypatch.setattr(app, '_requests', {})
    connection = MagicMock(side_effect=AssertionError('DB 연결 금지'))
    monkeypatch.setattr(psycopg, 'connect', connection)
    client = TestClient(app.app)
    try:
        assert client.get('/api/auth/me').status_code == 503
        response = client.get('/api/search')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        connection.assert_not_called()
    finally:
        client.close()


def test_writer_connection_uses_project_db_and_transactions(monkeypatch, tmp_path):
    connect = MagicMock()
    monkeypatch.setattr(psycopg, 'connect', connect)
    monkeypatch.delenv('BIGKINDS_DSN', raising=False)
    monkeypatch.setenv('BIGKINDS_ENV_FILE', str(tmp_path / 'missing.env'))
    with pytest.raises(accounts.HTTPException) as caught:
        with accounts.database():
            pass
    assert caught.value.status_code == 503
    connect.assert_not_called()
    monkeypatch.setenv('BIGKINDS_DSN', 'dbname=mock user=owner')
    with accounts.database():
        pass
    options = connect.call_args.kwargs
    assert options['autocommit'] is False and options['application_name'] == 'dice_accounts'
    assert 'read_only=off' not in options['options']


def test_schema_matches_migration():
    db_dir = MIGRATION.parent.parent
    migration = MIGRATION.read_text(encoding='utf-8').split('CREATE TABLE users', 1)[1].split('COMMIT;', 1)[0].strip()
    schema = (db_dir / 'schema.sql').read_text(encoding='utf-8').split('CREATE TABLE users', 1)[1].split('COMMIT;', 1)[0].strip()
    assert migration == schema


@pytest.fixture
def gated(http, monkeypatch):
    client, db = http
    monkeypatch.setattr(accounts, '_session_cache', {})
    for path in ('/api/search', '/api/stats', '/api/timeline', '/api/publishers'):
        monkeypatch.setitem(app.API_HANDLERS, path, lambda _query: ({'ok': True}, 200))
    monkeypatch.setattr(app, 'handle_query', lambda *_args, **_kwargs: ('<p>classic</p>', 200))
    monkeypatch.setattr(app, 'frontend_file', lambda _path: None)
    return client, db


def test_require_login_off_keeps_data_open(gated, monkeypatch):
    client, _ = gated
    monkeypatch.delenv('REQUIRE_LOGIN', raising=False)
    assert client.get('/api/search?q=관세').status_code == 200
    assert client.get('/classic').status_code == 200


def test_require_login_gates_data_until_session(gated, monkeypatch):
    client, db = gated
    monkeypatch.setenv('REQUIRE_LOGIN', '1')
    for path in ('/api/search?q=관세', '/api/stats?q=관세', '/api/timeline?q=관세', '/api/publishers'):
        response = client.get(path)
        assert response.status_code == 401 and response.json() == {'error': '로그인이 필요합니다.'}
    assert client.get('/classic').status_code == 401 and client.get('/').status_code == 401
    assert signup(client).status_code == 201  # auth 경로는 예외
    assert client.get('/api/auth/me').status_code == 401
    assert login(client).status_code == 200
    assert client.get('/api/search?q=관세').status_code == 200
    assert client.get('/api/stats?q=관세').status_code == 200
    assert client.get('/api/publishers').status_code == 200 and client.get('/classic').status_code == 200
    client.post('/api/auth/logout')
    assert client.get('/api/search?q=관세').status_code == 401
    assert client.get('/api/stats?q=관세').status_code == 401
    login(client)
    now = datetime.now(timezone.utc)
    db.execute('UPDATE user_sessions SET created_at = %s, expires_at = %s', (now - timedelta(days=15), now - timedelta(seconds=1)))
    db.raw.commit()
    accounts.forget_sessions()
    assert client.get('/api/timeline?q=관세').status_code == 401
    client.cookies.set(accounts.COOKIE, 'x' * 43)
    assert client.get('/api/publishers').status_code == 401
