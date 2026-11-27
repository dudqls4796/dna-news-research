"""뉴스 검색 JSON 및 빌드 결과의 HTTP 계약을 소켓 없이 검사한다."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from search import app, engine


def test_frontend_dist_environment_variable(fixture_dir):
    pytest.importorskip('httpx')
    release = fixture_dir / 'release'
    release.mkdir()
    (release / 'index.html').write_text('release build', encoding='utf-8')
    script = "from fastapi.testclient import TestClient; from search.app import app; " \
             "response = TestClient(app).get('/'); assert response.status_code == 200; " \
             "assert response.text == 'release build'"
    env = {**os.environ, 'FRONTEND_DIST': str(release)}
    env.pop('SEARCH_PASSWORD', None)
    subprocess.run([sys.executable, '-X', 'utf8', '-c', script], env=env, check=True)


@pytest.fixture
def fixture_dir():
    with TemporaryDirectory(prefix='dice-search-test-', dir=Path(__file__).resolve().parent) as directory:
        yield Path(directory)


@pytest.fixture
def client(monkeypatch, fixture_dir):
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app, 'FRONTEND_DIST', fixture_dir / 'dist')
    monkeypatch.setattr(app, '_requests', {})
    monkeypatch.delenv('SEARCH_PASSWORD', raising=False)
    # TestClient의 lifespan을 시작하지 않아 모델을 로드하지 않는다.
    http = TestClient(app.app)
    try:
        yield http
    finally:
        http.close()


@pytest.fixture
def news_result():
    rep = {'id': 5, 'title': '삼성전자 주가 상승', 'publisher': '동아일보',
           'url': 'https://example.com/5', 'published_date': '2026-10-03'}
    report = {**rep, 'id': 6, 'publisher': '한국일보', 'url': 'https://example.com/6'}
    follow = {**rep, 'id': 7, 'title': '삼성전자 주가 후속 보도', 'published_date': '2026-10-04'}
    event = {
        'group_id': 1, 'grouped': True, 'score': 0.9, 'date_from': '2026-10-03', 'date_to': '2026-10-03',
        'latest_match': '2026-10-03', 'representative': rep, 'publishers': ['동아일보', '한국일보'],
        'articles': [rep, report], 'article_count': 2,
    }
    params = engine.validate_params({
        'keywords': '삼성전자', 'q': '삼성전자 주가', 'date_from': '2026-10-01', 'date_to': '2026-10-31',
    })
    return {'params': params, 'events': [event], 'other_articles': [follow],
            'stats': {'basis': '관점 일치 기사', 'aspects': ['주가'],
                      'monthly': [{'month': '2026-10', 'count': 2,
                                   'top_keywords': [{'keyword': '코스피', 'mention_sum': 3, 'lift': 1.5}]}]}}


def test_search_ask_and_optional_followups(client, monkeypatch, news_result):
    def search(params, *, include_followups=False):
        assert params['ask'] == '지난달 삼성전자 주가'
        result = {**news_result, 'conversion': {'q': '삼성전자 주가', 'keywords': ['삼성전자']}}
        if include_followups:
            result['events'] = [{**news_result['events'][0], 'followups': news_result['other_articles']}]
        return result

    monkeypatch.setattr(app, 'search', search)
    query = {'ask': '지난달 삼성전자 주가'}
    legacy = client.get('/api/search', params=query)
    assert legacy.status_code == 200
    assert 'followups' not in legacy.json()['events'][0]
    response = client.get('/api/search', params={**query, 'followups': 1})
    assert response.status_code == 200
    data = response.json()
    assert data['conversion']['keywords'] == data['params']['keywords'] == ['삼성전자']
    assert data['events'][0]['representative']['url'] == 'https://example.com/5'
    assert data['events'][0]['score'] == 0.9
    assert data['events'][0]['articles'][1]['publisher'] == '한국일보'
    assert data['events'][0]['followups'] == data['other_articles']
    assert data['stats']['monthly'][0]['top_keywords'][0]['keyword'] == '코스피'
    assert data['stats']['aspects'] == ['주가']


def test_stats_endpoint_and_fast_search_options(client, monkeypatch, news_result):
    def search(params, *, include_followups=False, stats_mode='full'):
        assert params == {'keywords': '삼성전자', 'q': '삼성전자 주가'}
        assert include_followups
        if stats_mode == 'only':
            return {'stats': news_result['stats'], 'timings': {}, 'cache_hit': False}
        return {key: value for key, value in news_result.items() if key != 'stats' or stats_mode == 'full'}

    monkeypatch.setattr(app, 'search', search)
    query = {'kw': '삼성전자', 'q': '삼성전자 주가', 'followups': 1}
    legacy = client.get('/api/search', params=query).json()
    first = client.get('/api/search', params={**query, 'stats': 0}).json()
    stats = client.get('/api/stats', params=query).json()
    assert 'stats' not in first
    assert {**first, 'stats': stats['stats']} == legacy
    assert 'events' not in stats


def test_search_cache_uses_normalized_input_and_snapshot_and_reuses_stats_plan(client, monkeypatch, news_result):
    connection = Mock()
    connection.transaction.side_effect = nullcontext
    connection.execute.return_value.fetchone.return_value = {'transaction_read_only': 'on'}
    monkeypatch.setattr(engine.psycopg, 'connect', lambda **_: nullcontext(connection))
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: 'vector')
    monkeypatch.setattr(engine, 'infer_person_context', lambda _: None)
    monkeypatch.setattr(engine, '_search_result_cache', engine.OrderedDict())
    monkeypatch.setattr(engine, 'with_article_duplicates', lambda _, params, **__: params)
    snapshot = Mock(return_value=(('duplicates',), ('results',)))
    monkeypatch.setattr(engine, 'article_data_snapshot', snapshot)
    clock = Mock(return_value=10)
    monkeypatch.setattr(engine, 'monotonic', clock)
    expected_plan = (news_result['params'], {5: True}, {'삼성전자': ('삼성전자', False)}, {})
    collect = Mock(return_value=news_result['stats'])
    monkeypatch.setattr(engine, 'collect_core_stats', collect)
    search_db = Mock(side_effect=lambda *_args, **_kwargs: {
        'events': news_result['events'], 'timings': {}, '_stats_plan': expected_plan,
    })
    monkeypatch.setattr(engine, 'search_snapshot_results', search_db)
    query = {'kw': '삼성전자', 'q': ' 삼성전자 주가 '}
    first = client.get('/api/search', params={**query, 'stats': 0}).json()
    again = client.get('/api/search', params={**query, 'q': '삼성전자 주가', 'stats': 0}).json()
    assert not first['cache_hit'] and again['cache_hit'] and search_db.call_count == 1
    assert 'stats' not in first and '_stats_plan' not in first
    assert first['events'] == again['events']
    collect.assert_not_called()

    stats = client.get('/api/stats', params=query).json()
    assert stats['stats'] == news_result['stats']
    assert client.get('/api/stats', params=query).json()['cache_hit']
    collect.assert_called_once_with(connection, *expected_plan[:3])
    assert search_db.call_count == 1

    snapshot.return_value = (('duplicates',), ('keyword-update',))
    assert not client.get('/api/search', params={**query, 'stats': 0}).json()['cache_hit']
    monkeypatch.setattr(engine, 'QUERY_REVISION', 'test-revision')
    assert not client.get('/api/search', params={**query, 'stats': 0}).json()['cache_hit']
    clock.return_value += engine.SEARCH_RESULT_CACHE_SECONDS
    assert not client.get('/api/search', params={**query, 'stats': 0}).json()['cache_hit']
    assert search_db.call_count == 4


def test_login_search_shares_warmup_cache_under_uncommitted_article_write(client, monkeypatch, news_result):
    monkeypatch.setenv('REQUIRE_LOGIN', '1')
    monkeypatch.setattr(app, 'accounts_has_session', lambda request: request.cookies.get('session') == 'valid')
    monkeypatch.setattr(engine, '_article_version_server', None)
    monkeypatch.setattr(engine, '_article_pending_writers', {})
    monkeypatch.setattr(engine, '_article_commit_versions', [0] * 6)
    monkeypatch.setattr(engine, '_duplicate_snapshot', None)
    monkeypatch.setattr(engine, '_search_result_cache', engine.OrderedDict())
    versions = {'enabled': True, 'server': ['db'], 'last_article': 1858189, 'last_embedding': 1858189,
                'versions': [[index, index, 10, 0, 0] for index in range(6)],
                'pending_writers': [[5866767, table] for table in (0, 1, 2, 4)]}
    connections = []

    def connect(**_):
        conn = Mock()
        conn.transaction.side_effect = nullcontext
        connections.append(conn)

        def execute(sql, *_bindings):
            cursor = Mock()
            if sql.startswith('SHOW'):
                cursor.fetchone.return_value = {'transaction_read_only': 'on'}
            elif 'pg_stat_get_tuples_inserted' in sql:
                # 같은 쓰기 트랜잭션이 네 테이블을 잠근 8020 로그를 재현한다.
                unsafe = [False] * 6 if 'pg_visible_in_snapshot' in sql else [True, True, True, False, True, False]
                cursor.fetchone.return_value = {**versions, 'unsafe': unsafe}
            return cursor

        conn.execute.side_effect = execute
        return nullcontext(conn)

    monkeypatch.setattr(engine.psycopg, 'connect', connect)
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: 'vector')
    monkeypatch.setattr(engine, 'infer_person_context', lambda _: None)
    duplicates = Mock(return_value=[17])
    monkeypatch.setattr(engine, 'article_duplicate_ids', duplicates)
    search = Mock(return_value={'events': news_result['events'], 'stats': news_result['stats'], 'timings': {}})
    monkeypatch.setattr(engine, 'search_snapshot_results', search)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(engine.warm_duplicate_snapshot).result(timeout=5)

    assert client.get('/api/search', params={'q': '관세'}).status_code == 401
    client.cookies.set('session', 'valid')
    first = client.get('/api/search', params={'q': '관세'})
    again = client.get('/api/search', params={'q': '관세'})
    assert first.status_code == again.status_code == 200
    assert not first.json()['cache_hit'] and again.json()['cache_hit']
    assert first.json()['events'] == again.json()['events']
    assert first.json()['stats'] == again.json()['stats']
    assert len(connections) == 3
    duplicates.assert_called_once_with(connections[0])
    search.assert_called_once()
    assert search.call_args.args[1]['_duplicate_article_ids'] == [17]


@pytest.mark.parametrize('failure', [URLError('연결 실패'), TimeoutError('시간 초과')])
def test_question_model_failure_is_reported_and_not_cached(client, monkeypatch, failure):
    monkeypatch.setattr(engine, '_ask_cache', engine.OrderedDict())
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, 'question_publisher_pattern', lambda: engine.re.compile(r'(?!)'))
    request = Mock(side_effect=failure)
    monkeypatch.setattr(engine, 'urlopen', request)
    monkeypatch.setattr(engine.psycopg, 'connect', Mock(side_effect=AssertionError('DB 접근 금지')))

    for path in ('/api/search', '/api/timeline'):
        response = client.get(path, params={'ask': '대표 갈등'})
        assert response.status_code == 503
        assert '질문 해석 모델(Ollama)' in response.json()['error']
    assert request.call_count == 2
    assert not engine._ask_cache
    with pytest.raises(engine.QuestionConversionError):
        engine.search({'ask': '대표 갈등'})
    assert request.call_count == 3


def test_empty_question_still_returns_empty_search(client):
    response = client.get('/api/search', params={'ask': ' '})
    assert response.status_code == 200
    assert response.json()['params']['mode'] == 'empty'


def test_search_logs_original_exception_without_exposing_it(client, monkeypatch, caplog):
    connection = Mock()
    connection.transaction.side_effect = nullcontext
    connection.execute.return_value.fetchone.return_value = {'transaction_read_only': 'on'}
    connection.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(engine.psycopg, 'connect', lambda **_: nullcontext(connection))
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    error = engine.psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
    monkeypatch.setattr(engine, 'find_core_articles', Mock(side_effect=error))

    response = client.get('/api/search', params={'kw': '대표 갈등'})

    assert response.status_code == 400
    assert response.json() == {'error': '검색 실패: 로컬 모델 캐시·CUDA·DB 연결·읽기 권한을 확인하세요.'}
    assert any(record.name == engine.__name__ and record.exc_info and record.exc_info[1] is error
               for record in caplog.records)


def test_followups_use_current_read_only_search_and_keep_results_on_timeout(monkeypatch, news_result):
    connection = Mock()
    connection.transaction.side_effect = nullcontext
    connection.execute.return_value.fetchone.return_value = {'transaction_read_only': 'on'}
    connection.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(engine.psycopg, 'connect', lambda **_: nullcontext(connection))
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: 'vector')
    monkeypatch.setattr(engine, 'infer_person_context', lambda _: None)
    monkeypatch.setattr(engine, 'missing_article_coverage', lambda *_: None)
    monkeypatch.setattr(engine, '_search_db', lambda *_args, **_kwargs: {
        'events': [dict(news_result['events'][0])], 'timings': {},
    })
    event = news_result['events'][0]
    params = {'keywords': '삼성전자', 'q': '삼성전자 주가'}
    followups = Mock(return_value={engine.followup_key(event): news_result['other_articles']})
    monkeypatch.setattr(engine, 'event_followups', followups)
    assert 'followups' not in engine.search(params)['events'][0]
    followups.assert_not_called()
    result = engine.search(params, include_followups=True)
    assert result['read_only'] and connection.read_only is True
    assert result['events'][0]['followups'] == news_result['other_articles']
    assert followups.call_args.args[0] is connection
    followups.side_effect = engine.psycopg.errors.QueryCanceled()
    result = engine.search(params, include_followups=True)
    assert result['events'][0]['representative'] == event['representative']
    assert 'followups_error' in result


def test_timeline_json_and_classic_fragment(client, monkeypatch, news_result):
    search = Mock(return_value=news_result)
    monkeypatch.setattr(app, 'search', search)
    event = {**news_result['events'][0], 'event_index': 0, 'event_count': 1, 'date_label': '2026-10-03',
             'source_label': '동아일보 외 1개 언론사'}
    checks = [{'attempt': 1, 'reasons': []}]
    generated = {'items': [event], 'lead': '기사 제목 변경과 코스피 상승을 보도했다.', 'model': 'qwen3:4b',
                 'llm_ms': 200.5, 'llm_calls': 2, 'cached': False, 'summary_status': '통과', 'summary_checks': checks,
                 'months': [{'month': '2026-10', 'source': '규칙', 'text': '초 삼성전자 상승', 'checks': []}]}
    monkeypatch.setattr(app, 'generate_timeline', lambda _: generated)
    monkeypatch.setattr(app, 'timeline_articles', lambda *_: (
        {(True, 1): event['articles']}, {engine.followup_key(event): news_result['other_articles']},
    ))
    query = {'ask': '지난달 삼성전자 주가', 'kw': '삼성전자', 'q': '삼성전자 주가', 'resolved': 1}
    response = client.get('/api/timeline', params=query)
    assert response.status_code == 200
    params = search.call_args.args[0]
    assert params == {'keywords': '삼성전자', 'q': '삼성전자 주가'}
    assert search.call_args.kwargs == {'timeline': True, 'cover_months': True}
    data = response.json()
    assert data['header'] == '삼성전자 주가 · 2026-10 ~ 2026-10'
    assert data['months'] == [{**generated['months'][0], 'event_count': 1, 'article_count': 2}]
    assert data['items'][0]['date_label'] == '2026-10-03'
    assert data['items'][0]['source_label'] == '동아일보 외 1개 언론사'
    assert data['items'][0]['articles'] == event['articles']
    assert data['items'][0]['followups'] == news_result['other_articles']
    assert data['lead'] == generated['lead'] and data['summary_checks'] == checks
    assert data['llm_ms'] == 200.5 and data['llm_calls'] == 2
    del generated['months']
    fallback = client.get('/api/timeline', params=query).json()['months'][0]
    assert fallback['source'] == '제목' and '삼성전자' in fallback['text']
    html = client.get('/timeline', params={**query, 'fragment': 1})
    assert html.status_code == 200
    assert search.call_args.kwargs == {'timeline': True}
    assert '동일 보도 보기' in html.text and '후속 기사 보기' in html.text and '<!doctype' not in html.text


def test_search_errors_and_empty_timeline(client, monkeypatch, news_result):
    for path in ('/api/search', '/api/timeline'):
        response = client.get(path, params={'q': '가' * 201})
        assert response.status_code == 400
        assert response.json()['error']

    result = {**news_result, 'data_absence': 'scope_empty', 'events': []}
    monkeypatch.setattr(app, 'search', lambda *_args, **_kwargs: result)
    query = {'q': '삼성전자', 'publisher': '__others__'}
    assert client.get('/api/search', params=query).json()['data_absence'] == 'scope_empty'
    data = client.get('/api/timeline', params=query).json()
    assert data['months'] == data['items'] == []
    assert data['notice'] == app.DATA_ABSENCE_MESSAGES['scope_empty']
    del result['data_absence']
    result['ai_error'] = '로컬 AI에 연결하지 못했습니다.'
    response = client.get('/api/timeline', params=query)
    assert response.status_code == 503
    assert response.json() == {'error': result['ai_error']}


def test_publishers_and_existing_authentication(client, monkeypatch):
    rows = [{'publisher': '동아일보', 'article_count': 30}, {'publisher': '한국일보', 'article_count': 50},
            {'publisher': 'KBS', 'article_count': 40}]
    monkeypatch.setattr(app, 'list_publishers', lambda: rows)
    monkeypatch.setenv('SEARCH_PASSWORD', '시험용')
    guarded = ['/api/publishers', '/api/search', '/api/timeline', '/classic', '/timeline', '/']
    for path in guarded:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers['www-authenticate'].startswith('Basic ')
    token = base64.b64encode('user:시험용'.encode()).decode()
    response = client.get('/api/publishers', headers={'Authorization': f'Basic {token}'})
    assert response.status_code == 200
    assert response.json() == {'publishers': [
        {'value': '동아일보', 'label': '동아일보'}, {'value': '한국일보', 'label': '한국일보'},
        {'value': '__others__', 'label': '기타 언론사'},
    ]}
    monkeypatch.delenv('SEARCH_PASSWORD')
    assert client.get('/api/search').json()['events'] == []


def test_frontend_assets_and_classic_fallback(client, monkeypatch, fixture_dir):
    monkeypatch.setattr(app, 'ollama_available', lambda: True)
    monkeypatch.setattr(app, 'list_publishers', lambda: [])
    assert 'action="/"' in client.get('/').text
    assert 'action="/classic"' in client.get('/classic').text
    dist = app.FRONTEND_DIST
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<div id="root"></div>', encoding='utf-8')
    (dist / 'assets' / 'index-a1.js').write_text('void 0', encoding='utf-8')
    (dist / 'dice-logo-horizontal.png').write_bytes(b'logo fixture')
    (fixture_dir / 'private.txt').write_text('private fixture', encoding='utf-8')
    monkeypatch.setenv('SEARCH_PASSWORD', '시험용')
    response = client.get('/', params={'ask': '삼성전자'})
    assert response.status_code == 200 and '<div id="root">' in response.text
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert "img-src 'self'" in response.headers['content-security-policy']
    script = client.get('/assets/index-a1.js')
    assert script.text == 'void 0' and script.headers['content-type'].startswith('text/javascript')
    assert client.get('/dice-logo-horizontal.png').content == b'logo fixture'
    assert client.head('/assets/index-a1.js').content == b''
    assert client.get('/classic').status_code == 401
    assert client.get('/api/publishers').status_code == 401
    monkeypatch.delenv('SEARCH_PASSWORD')
    for path in ('/assets/missing.js', '/assets/%2e%2e/index.html', '/assets/%2e%2e/%2e%2e/private.txt',
                 '/assets/%2e%2e%5cindex.html', '/assets/index-a1.js%3Aother'):
        assert client.get(path).status_code == 404
