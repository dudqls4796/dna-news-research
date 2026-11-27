"""기간·언론사 해석, 공통 SQL, 수동 수정 및 장애 복귀 검사."""
from datetime import date
from io import BytesIO
import json
import re
from threading import Event
from urllib.parse import urlencode, urlsplit, parse_qs
from unittest.mock import Mock, patch

import pytest

from search import engine, app, prompts


@pytest.fixture(autouse=True)
def disable_live_tavily(monkeypatch):
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')


def test_advanced_search_open_state_and_summary():
    def advanced(page):
        return page.split('<details class="panel advanced-search"', 1)[1].split('</details>', 1)[0]

    empty = advanced(app.render_page({}))
    assert empty.startswith('>') and '조건 없음' in empty
    values = {'kw': '김도영', 'q': '기업인 김도영', 'date_from': '', 'date_to': '', 'publisher': ''}
    direct = advanced(app.render_page(values))
    assert direct.startswith(' open>')
    assert '필수 키워드 김도영 · 주제 기업인 김도영 · 기간 전체 · 언론사 전체' in direct
    for key in ('kw', 'q', 'date_from', 'date_to', 'publisher'):
        assert f'name="{key}"' in direct
    question = advanced(app.render_page({**values, 'ask': '김도영 누구야'}))
    assert question.startswith('>') and '필수 키워드 김도영' in question
    dated = advanced(app.render_page({'date_to': '2026-11-19', 'publisher': '<신문>'}))
    assert dated.startswith(' open>') and '기간 처음 ~ 2026-11-19 · 언론사 &lt;신문&gt;' in dated


def test_publisher_select_conversion_and_clear(monkeypatch):
    monkeypatch.setattr(app, 'ollama_available', lambda: True)
    monkeypatch.setattr(app, 'list_publishers', lambda: [
        {'publisher': '한겨레', 'article_count': 3000}, {'publisher': '동아일보', 'article_count': 2000},
        {'publisher': '4th.kr', 'article_count': 1500}, {'publisher': '작은신문', 'article_count': 10}])
    params = engine.validate_params({'keywords': '반도체', 'publisher': '<외부신문>'})
    conversion = dict(keywords=['반도체'], q='', publisher='<외부신문>', removed_keywords=[], llm_ms=1)
    result = {'params': params, **engine.empty_search_result(''), 'elapsed_ms': 0, 'conversion': conversion}
    calls = []
    def search(args, **kwargs):
        calls.append(args)
        return result if 'ask' in args else {**result, 'params': engine.validate_params(args), 'conversion': None}
    monkeypatch.setattr(app, 'search', search)
    html, status = app.handle_query(urlencode({'ask': '외부신문 반도체'}))
    assert status == 200
    select = html.split('<select id="publisher"', 1)[1].split('</select>', 1)[0]
    assert '<option value="">전체 언론사</option>' in select
    assert 'value="동아일보"' in select and 'value="한겨레"' in select
    major, other = select.split('</optgroup>')
    assert '<optgroup label="주요 언론사">' in major
    assert major.index('동아일보') < major.index('한겨레') < major.index('작은신문')
    assert '4th.kr' not in select and '기타 언론사' in other
    assert 'value="&lt;외부신문&gt;" selected' in select
    assert '언론사 &lt;외부신문&gt;' in html
    assert 'within_publisher' not in html
    _, status = app.handle_query(urlencode({'kw': '반도체', 'publisher': ''}))
    assert status == 200 and 'publisher' not in calls[-1]


def test_publisher_list_reads_db_and_uses_cache(monkeypatch):
    monkeypatch.setattr(engine, '_publisher_cache', (0.0, []))
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    with patch.object(engine.psycopg, 'connect') as connect:
        conn = connect.return_value.__enter__.return_value
        rows = [{'publisher': '동아일보', 'article_count': 2000}, {'publisher': '한겨레', 'article_count': 1000}]
        conn.execute.return_value.fetchall.return_value = rows
        assert engine.list_publishers() == rows
        assert engine.list_publishers() == rows
        assert connect.call_count == 1
        sql = conn.execute.call_args.args[0]
        assert 'FROM article_sources' in sql and 'HAVING count(*) >= 10' in sql
        assert 'ORDER BY article_count DESC, publisher' in sql
        assert 'default_transaction_read_only=on' in connect.call_args.kwargs['options']


def test_publisher_groups_limit_and_preserve_omitted_selection():
    publishers = [{'publisher': f'신문{chr(0xac00 + i)}', 'article_count': 1000 - i} for i in range(32)]
    publishers += [{'publisher': 'abcn.kr', 'article_count': 50}]
    html = app.render_page({'_publishers': publishers, 'publisher': '<소수언론사>'})
    select = html.split('<select id="publisher"', 1)[1].split('</select>', 1)[0]
    major, other = select.split('</optgroup>')
    assert major.count('<option ') == 31  # 전체 언론사와 주요 30개
    assert '신문가' in major and '신문힣' not in major
    assert '신문' + chr(0xac00 + 30) not in other
    assert 'abcn.kr' not in other and '기타 언론사' in other
    assert 'value="&lt;소수언론사&gt;" selected' in other


def test_other_publishers_scope_applies_to_search_stats_timeline_and_links(monkeypatch):
    rows = [{'publisher': '동아일보', 'article_count': 100}, {'publisher': '한겨레', 'article_count': 80},
            {'publisher': 'KBS', 'article_count': 60}]
    monkeypatch.setattr(engine, 'list_publishers', lambda: rows)
    params = engine.validate_params({'keywords': '반도체', 'publisher': '__others__'})
    for sql, values in (engine.article_filter(params), engine.timeline_article_filter(params),
                        engine.stats_query(['반도체'], params=params)):
        assert 'NOT (fs.publisher = ANY(%s::text[]))' in sql
        assert ['동아일보', '한겨레'] in values and '__others__' not in values
    html = app.render_page({'kw': '반도체', 'publisher': '__others__', '_publishers': rows})
    assert 'value="__others__" selected>기타 언론사' in html
    assert '언론사 기타 언론사' in html and 'value="KBS"' not in html
    assert parse_qs(urlsplit(app.result_url({'kw': '반도체', 'publisher': '__others__'})).query)['publisher'] == ['__others__']
    selected = app.render_page({'publisher': 'KBS', '_publishers': rows})
    assert 'value="KBS" selected' in selected
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    with pytest.raises(engine.SearchError):
        engine.article_filter(params)


def test_periods_and_invalid_fields():
    assert engine.explicit_question_period('지난달 동아일보 기사', date(2026, 11, 18)) == ('2026-10-01', '2026-10-31')
    assert engine.explicit_question_period('지난달 기사', date(2026, 1, 2)) == ('2025-12-01', '2025-12-31')
    assert engine.explicit_question_period('지난달', date(2028, 3, 1)) == ('2028-02-01', '2028-02-29')
    assert engine.explicit_question_period('2026년 10월 삼성전자 주가', date(2026, 11, 18)) == ('2026-10-01', '2026-10-31')
    assert engine.explicit_question_period('2026년 10월 3일 기사', date(2026, 11, 18)) == ('2026-10-03', '2026-10-03')
    assert engine.explicit_question_period('삼성전자 주가', date(2026, 11, 18)) is None
    raw = {'keywords': [], 'q': '주가', 'date_from': '2026-02-30', 'date_to': 3, 'publisher': []}
    result = engine.postprocess_question(raw, lambda _: {})
    assert result['date_from'] is result['date_to'] is result['publisher'] is None
    for fields in ({'date_from': '2026-02-30'}, {'date_from': '2026-11-01', 'date_to': '2026-10-01'},
                   {'publisher': ['동아일보']}, {'date_to': 12}):
        with pytest.raises(engine.SearchError):
            engine.validate_params({'q': '주제', **fields})


@pytest.mark.parametrize(('question', 'today', 'expected'), [
    ('지난달 09일', date(2026, 11, 24), '2026-10-09'),
    ('이번 달 09일 기사', date(2026, 11, 24), '2026-11-09'),
    ('10월 09일', date(2026, 11, 24), '2026-10-09'),
    ('지난 달 09일 뉴스', date(2026, 1, 24), '2025-12-09'),
])
def test_month_day_without_topic_uses_single_day_digest(question, today, expected):
    with patch.object(engine, 'question_publisher_pattern', return_value=re.compile(r'(?!)')), \
            patch.object(engine, 'ollama_chat') as chat:
        result = engine.convert_question(question, today)
    chat.assert_not_called()
    assert (result['date_from'], result['date_to'], result['keywords']) == (expected, expected, [])
    params = {key: result[key] for key in ('q', 'keywords', 'date_from', 'date_to')}
    assert engine.validate_params(params)['mode'] == 'digest'
    assert '09일' not in result['q']


def test_month_day_removal_preserves_numeric_compounds():
    ask = '지난달 09일 주 4일제 3일장'
    today = date(2026, 11, 24)
    result = engine.ground_question_filters({'q': ask, 'keywords': ['09일', '4일제', '3일장']}, ask, today)
    assert (result['date_from'], result['date_to']) == ('2026-10-09', '2026-10-09')
    assert result['q'] == '주 4일제 3일장'
    assert result['keywords'] == ['4일제', '3일장']


def test_duplicate_ids_are_excluded_before_monthly_and_event_counts():
    with patch.object(engine, 'article_duplicate_ids', return_value=[65, 66]) as find_duplicates:
        params = engine.with_article_duplicates(None, engine.validate_params({
            'q': '뉴스', 'date_from': '2026-10-09', 'date_to': '2026-10-09',
        }))
    find_duplicates.assert_called_once_with(None, '2026-10-09', '2026-10-09')
    digest_sql, digest_values = engine.candidate_query(params, None)
    monthly_sql, monthly_values = engine.stats_query([], [], params, core_scope=True)
    exclusion = 'a.id NOT IN (SELECT unnest(%s::bigint[]))'
    assert digest_sql.index(exclusion) < digest_sql.index('count(*) AS article_count')
    assert monthly_sql.index(exclusion) < monthly_sql.index('count(*) AS count')
    assert [65, 66] in digest_values and [65, 66] in monthly_values


def test_duplicate_cache_tracks_article_and_source_versions_only():
    conn = Mock()
    versions = [[index, index, 10, 0, 0] for index in range(6)]
    row = {'enabled': True, 'server': ['database', 'reader'], 'last_article': 10, 'last_embedding': 10,
           'versions': versions, 'unsafe': [False] * 6, 'pending_writers': []}
    article_edit = {**row, 'versions': [[0, 0, 10, 1, 0], *versions[1:]]}
    source_delete = {**article_edit, 'versions': [article_edit['versions'][0], [1, 1, 10, 0, 1], *versions[2:]]}
    conn.execute.return_value.fetchone.side_effect = [
        row, {**row, 'unrelated_write': 'user_sessions'}, article_edit, source_delete,
        {**source_delete, 'unsafe': [False, True, False, False, False, False]},
    ]
    with (patch.object(engine, '_duplicate_snapshot', None),
          patch.object(engine, 'article_duplicate_ids', side_effect=[[1], [2], [3], [4], [5]]) as find):
        engine.with_article_duplicates(conn, {}, reuse_snapshot=True)['_duplicate_article_ids'].append(99)
        assert engine.with_article_duplicates(conn, {}, reuse_snapshot=True)['_duplicate_article_ids'] == [1]
        assert engine.with_article_duplicates(conn, {}, reuse_snapshot=True)['_duplicate_article_ids'] == [2]
        assert engine.with_article_duplicates(conn, {}, reuse_snapshot=True)['_duplicate_article_ids'] == [3]
        assert engine.with_article_duplicates(conn, {}, reuse_snapshot=True)['_duplicate_article_ids'] == [4]
        scope = {'date_to': '2026-11-25'}
        assert engine.with_article_duplicates(conn, scope, reuse_snapshot=True)['_duplicate_article_ids'] == [5]
    assert conn.execute.call_count == 5 and find.call_count == 5
    find.assert_called_with(conn, None, '2026-11-25')
    sql, bindings = conn.execute.call_args.args
    assert 'pg_visible_in_snapshot' in sql
    assert set(bindings[0]) == {'articles', 'article_sources', 'article_keywords', 'article_groups',
                                'article_categories', 'article_embeddings'}


def test_article_snapshot_uses_visible_insert_and_search_only_relations():
    conn = Mock()
    versions = [[index, index, 10, 0, 0] for index in range(6)]
    row = {'enabled': True, 'server': ['database', 'reader'], 'last_article': 10, 'last_embedding': 10,
           'versions': versions, 'unsafe': [False] * 6, 'pending_writers': []}
    conn.execute.return_value.fetchone.side_effect = [
        row, {**row, 'last_article': 12},
        {**row, 'versions': [*versions[:-1], [5, 5, 10, 1, 0]]},
        {**row, 'last_embedding': 12},
        {**row, 'unsafe': [False, False, False, True, False, False]},
        {**row, 'enabled': False},
    ]
    duplicate, search = engine.article_data_snapshot(conn)
    fresh_duplicate, fresh_search = engine.article_data_snapshot(conn)
    assert duplicate != fresh_duplicate and search != fresh_search
    embedded_duplicate, embedded_search = engine.article_data_snapshot(conn)
    assert duplicate == embedded_duplicate and search != embedded_search
    new_embedding_duplicate, new_embedding_search = engine.article_data_snapshot(conn)
    assert duplicate == new_embedding_duplicate and search != new_embedding_search
    assert engine.article_data_snapshot(conn) == (duplicate, None)
    assert engine.article_data_snapshot(conn) == (None, None)
    conn.execute.return_value.fetchone.side_effect = None
    conn.execute.return_value.fetchone.return_value = {**row, 'last_article': 12}
    assert engine.stable_article_data_snapshot(conn, (duplicate, search)) == (None, None)


@pytest.mark.parametrize(('table', 'status', 'duplicate_changed', 'result_changed'), [
    (0, 'committed', True, True),
    (1, 'committed', True, True),
    (3, 'committed', False, True),
    (1, 'aborted', False, False),
    (3, 'in progress', False, False),
])
def test_article_writer_completion_is_checked_before_stats_catch_up(
        monkeypatch, table, status, duplicate_changed, result_changed):
    monkeypatch.setattr(engine, '_article_version_server', None)
    monkeypatch.setattr(engine, '_article_pending_writers', {})
    monkeypatch.setattr(engine, '_article_commit_versions', [0] * 6)
    conn = Mock()
    unchanged = {'enabled': True, 'server': ['locked-db'], 'last_article': 10, 'last_embedding': 10,
                 'versions': [[index, index, 10, 0, 0] for index in range(6)],
                 'unsafe': [False] * 6, 'pending_writers': []}
    pending = {**unchanged, 'pending_writers': [[5866767, table]]}
    conn.execute.return_value.fetchone.side_effect = [unchanged, pending, pending, unchanged, unchanged]
    conn.execute.return_value.fetchall.return_value = [{'xid': 5866767, 'status': status}]
    before = engine.article_data_snapshot(conn)
    assert engine.article_data_snapshot(conn) == before
    assert engine.article_data_snapshot(conn) == before
    duplicate, result = engine.article_data_snapshot(conn)
    assert (duplicate != before[0]) is duplicate_changed
    assert (result != before[1]) is result_changed
    assert engine.article_data_snapshot(conn) == (duplicate, result)


def test_search_result_cache_evicts_least_recently_used_and_copies_payload(monkeypatch):
    monkeypatch.setattr(engine, '_search_result_cache', engine.OrderedDict())
    monkeypatch.setattr(engine, 'SEARCH_RESULT_CACHE_LIMIT', 2)
    result = {'events': [{'id': 1}]}
    engine.cache_search_result('first', result, None)
    engine.cache_search_result('second', result, None)
    result['events'][0]['id'] = 99
    entry = engine.cached_search_result('first')
    assert entry == ({'events': [{'id': 1}]}, None)
    entry[0]['events'].clear()
    engine.cache_search_result('third', result, None)
    assert engine.cached_search_result('first') == ({'events': [{'id': 1}]}, None)
    assert engine.cached_search_result('second') is None


@pytest.mark.parametrize(('question', 'today', 'start', 'end', 'topic'), [
    ('이번주 경제 뉴스', date(2026, 11, 20), '2026-11-16', '2026-11-20', '경제 뉴스'),
    ('이번 주 경제 뉴스', date(2026, 1, 1), '2025-12-29', '2026-01-01', '경제 뉴스'),
    ('지난 주 기사', date(2026, 11, 16), '2026-11-09', '2026-11-15', '기사'),
    ('최근 3일 소식', date(2026, 11, 20), '2026-11-18', '2026-11-20', '소식'),
    ('최근 2주 기사', date(2026, 11, 20), '2026-11-07', '2026-11-20', '기사'),
])
def test_relative_periods_override_model_dates(question, today, start, end, topic):
    raw = {'keywords': [], 'q': question, 'date_from': '2026-11-01', 'date_to': '2026-11-30'}
    result = engine.ground_question_filters(raw, question, today)
    assert (result['date_from'], result['date_to'], result['q']) == (start, end, topic)
    assert engine.explicit_question_period('이번주부터 다음주까지', today) is None
    assert engine.explicit_question_period('최근 0일', today) is None


def test_news_digest_conversion_keeps_subject_and_scope():
    with patch.object(engine, 'ollama_chat') as chat:
        result = engine.convert_question('이번주 경제 뉴스', date(2026, 11, 20))
        yesterday = engine.convert_question('어제 뉴스', date(2026, 11, 20))
    assert result['q'] == '경제 뉴스' and result['keywords'] == []
    assert yesterday['q'] == '뉴스' and yesterday['date_from'] == yesterday['date_to'] == '2026-11-19'
    chat.assert_not_called()
    for topic, category in [('뉴스', None), ('경제 뉴스', '경제'), ('소식', None), ('기사', None)]:
        params = engine.validate_params({'q': topic, 'date_from': result['date_from'], 'date_to': result['date_to']})
        sql, values = engine.candidate_query(params, None)
        assert params['mode'] == 'digest'
        assert 'embedding' not in sql and 'PARTITION BY grouped, event_id' in sql
        assert 'ORDER BY routine_title, publisher_count DESC, article_count DESC' in sql
        assert sql.count('%s') == len(values)
        assert (category == '경제') == ('경제' in values)
    params = engine.validate_params({'q': '경제 성장 뉴스', 'date_from': result['date_from']})
    assert params['mode'] == 'topic'


@pytest.mark.parametrize(('core_size', 'partition', 'needs_semantic'), [
    (engine.VECTOR_CANDIDATES - 1, None, True),
    (engine.VECTOR_CANDIDATES, None, False),
    (engine.VECTOR_CANDIDATES, 'category', True),
    (engine.VECTOR_CANDIDATES, 'title', True),
])
def test_topic_search_only_fetches_semantic_seed_when_it_can_change_ranking(core_size, partition, needs_semantic):
    params = engine.validate_params({'q': '관세', 'date_from': '2025-01-01', 'date_to': '2026-11-25'})
    if partition == 'category':
        params['excluded_categories'] = ['스포츠']
    title_flag = ('a.title ~* %s', ('관세',)) if partition == 'title' else ('', ())
    core = {article_id: bool(article_id % 2) for article_id in range(1, core_size + 1)}
    semantic_id = core_size + 1
    conn = Mock()
    conn.execute.return_value.fetchall.side_effect = [[{'id': semantic_id}], []] if needs_semantic else [[]]

    with (patch.object(engine, 'find_core_articles', return_value=core),
          patch.object(engine, 'required_title_flag', return_value=title_flag),
          patch.object(engine, 'candidate_query', wraps=engine.candidate_query) as candidate_query):
        engine._search_db(conn, params, '[1]', count_only=True)

    assert conn.execute.call_count == candidate_query.call_count == (2 if needs_semantic else 1)
    if needs_semantic:
        assert candidate_query.call_args_list[0].args == (params, '[1]')
    ranking_params, _, ids = candidate_query.call_args.args
    assert ranking_params['mode'] == 'combined'
    assert ids == ([semantic_id] if needs_semantic else []) + list(core)
    assert candidate_query.call_args.kwargs['core'] == core
    ranking_sql = conn.execute.call_args.args[0]
    assert 'core_match DESC, title_core DESC' in ranking_sql


@pytest.mark.parametrize(('title_count', 'core_count', 'other_options', 'scored_count'), [
    (200, 210, {}, 200),
    (199, 210, {}, 210),
    (150, 199, {}, 220),
    (200, 210, {'supporting_terms': ['반도체']}, 220),
    (200, 210, {'excluded_categories': ['스포츠']}, 220),
])
def test_combined_distance_prunes_only_candidates_below_cutoff(title_count, core_count, other_options, scored_count):
    params = {**engine.validate_params({'keywords': '삼성전자', 'q': '삼성전자 반도체'}), **other_options}
    ids = list(range(1, 221))
    core = {article_id: article_id <= title_count for article_id in ids[:core_count]}

    sql, values = engine.candidate_query(params, '[1]', ids, core=core)

    scoring_source = sql.index('FROM unnest(%s::bigint[]) c(article_id) JOIN articles')
    assert values[sql[:scoring_source].count('%s')] == ids[:scored_count]
    assert ('candidate_total' in sql) == (scored_count < len(ids))
    if scored_count < len(ids):
        assert values[0] == ids
    assert sql.count('%s') == len(values)


def test_unbounded_keyword_candidates_filter_duplicate_ids_without_article_join():
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [{'article_id': 20}]
    params = {**engine.validate_params({'q': '삼성전자 반도체'}), '_duplicate_article_ids': [65]}

    assert engine.keyword_candidate_ids(conn, params) == [20]

    sql, values = conn.execute.call_args.args
    assert 'a.title LIKE ANY' in sql and ['%삼%성%전%자%', '%삼%전%'] in values
    assert 'JOIN articles' not in sql and 'c.article_id NOT IN' in sql
    assert values[-1] == [65] and sql.count('%s') == len(values)


def test_scoped_single_core_term_applies_date_and_publisher_to_both_sources():
    params = engine.validate_params({'q': '관세', 'date_from': '2025-01-01', 'date_to': '2026-11-25',
                                     'publisher': '동아일보'})
    params['_duplicate_article_ids'] = [65, 66]
    sql, values = engine.core_article_query(params, None, engine.core_terms(params))

    assert sql.count('a.title ~* %s') == 1
    assert sql.count('a.published_date >= %s::date') == sql.count('fs.publisher=%s') == 2
    assert 'UNION ALL' in sql and 'bool_or(title_core)' in sql
    assert 'AND id NOT IN (SELECT unnest(%s::bigint[]))' in sql
    assert values[-1] == [65, 66] and sql.count('%s') == len(values)


@pytest.mark.parametrize(('prefix', 'year'), [('', 2026), ('작년 ', 2025)])
def test_question_month_year_correction(monkeypatch, prefix, year):
    raw = {'keywords': [], 'q': '삼성전자 주가', 'date_from': '2025-10-01', 'date_to': '2025-10-31'}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1.0))
    with patch.dict(engine._ask_cache, clear=True):
        result = engine.convert_question(f'{prefix}10월 삼성전자 주가', date(2026, 11, 20))
    assert (result['date_from'], result['date_to']) == (f'{year}-10-01', f'{year}-10-31')


def test_question_filters_cache_uses_search_date_and_think(monkeypatch):
    monkeypatch.delenv('LLM_THINK', raising=False)
    raw = {'keywords': ['반도체'], 'q': '반도체 수출', 'date_from': None, 'date_to': None, 'publisher': '동아일보'}
    original = engine.postprocess_question
    with patch.object(engine, 'ollama_chat', return_value=(raw, 1.0)) as chat, patch.object(
            engine, 'postprocess_question', side_effect=lambda r: original(r, lambda _: {'반도체': 10})), patch.dict(
            engine._ask_cache, clear=True):
        result = engine.convert_question('지난달 동아일보에서 나온 반도체 수출 기사', date(2026, 11, 18))
        assert (result['date_from'], result['date_to'], result['publisher']) == ('2026-10-01', '2026-10-31', None)
        assert engine.convert_question('지난달 동아일보에서 나온 반도체 수출 기사', date(2026, 11, 18))['cached']
        assert engine.convert_question('지난달 동아일보에서 나온 반도체 수출 기사', date(2026, 12, 1))['date_from'] == '2026-11-01'
        assert chat.call_count == 2
        assert chat.call_args.args == (
            prompts.QUERY_CONVERSION,
            {'question': '지난달 나온 반도체 수출 기사', 'search_date': '2026-12-01'},
            engine.ASK_SCHEMA,
        )
        monkeypatch.setenv('LLM_THINK', 'true')
        assert not engine.convert_question('지난달 동아일보에서 나온 반도체 수출 기사', date(2026, 12, 1))['cached']
        assert chat.call_count == 3


@pytest.mark.parametrize(('question', 'keyword', 'topic'), [
    ('삼전 주가 왜 떨어졌어', '삼성전자', '삼성전자 주가 하락 원인'),
    ('국대 축구 불화설', '국가대표', '국가대표 축구 불화설'),
    ('한은의 금리 발표', '한국은행', '한국은행 금리 발표'),
])
def test_registered_query_aliases_keep_grounding(question, keyword, topic):
    with patch.object(engine, 'ollama_chat') as chat, patch.object(engine, 'web_query_conversion') as web:
        result = engine.convert_question(question)
    chat.assert_not_called()
    web.assert_not_called()
    assert result['q'] == topic and result['keywords'] == [keyword] and result['web_query'] is None
    assert engine.grounded_topic(topic, question) == topic
    assert engine.grounded_topic(topic + ' 전망', question) == topic
    assert engine.grounded_topic(topic + '!', question) == topic
    assert engine.grounded_topic('국가대표 불화설', '대한민국국대 불화설') == '대한민국국대 불화설'
    raw_alias = next(alias for alias, standard in prompts.QUERY_ALIASES.items() if standard == keyword)
    sql, values = engine.keyword_source([keyword])
    assert 'keyword=ANY' in sql and {raw_alias, keyword} <= set(values[0])
    result = engine.postprocess_question({'keywords': [raw_alias, keyword], 'q': topic}, lambda _: {keyword: 1})
    assert result['keywords'] == [keyword]


def test_conversion_restores_missing_terms_after_removing_filters():
    raw = {'keywords': [], 'q': '월드컵 일정', 'publisher': '동아일보'}
    with patch.object(engine, 'ollama_chat', return_value=(raw, 1.0)), patch.dict(engine._ask_cache, clear=True):
        result = engine.convert_question('지난주 동아일보에서 월드컵 한국 경기 일정 알려줘', date(2026, 11, 19))
    assert set(result['q'].split()) == {'월드컵', '한국', '경기', '일정'}
    assert (result['date_from'], result['date_to'], result['publisher']) == ('2026-11-09', '2026-11-15', None)
    raw = {'keywords': [], 'q': '국대 축구'}
    with patch.object(engine, 'ollama_chat', return_value=(raw, 1.0)), patch.dict(engine._ask_cache, clear=True):
        result = engine.convert_question('지난주 국대 축구 불화설', date(2026, 11, 19))
    assert result['q'] == '국가대표 축구 불화설' and result['keywords'] == ['국가대표']


@pytest.mark.parametrize('mode', ('tool', 'structured-tool', 'timeout', 'no-key', 'no-call', 'known', 'unrelated-tool'))
def test_query_web_tool_and_fallback(mode, monkeypatch):
    question = '미래솔 신제품 공개 일정'
    query = '미래솔 <명칭>'
    final = {'keywords': [], 'q': '미래솔 신제품', 'date_from': None, 'date_to': None, 'publisher': None,
             'resolved_alias': {'term': '미래솔', 'canonical': '미래솔루션'}}
    used = mode in ('tool', 'structured-tool')
    calls = []

    def json_response(data):
        return BytesIO(json.dumps(data).encode('utf-8'))

    def mock_urlopen(request, timeout):
        data = json.loads(request.data)
        calls.append((request.full_url, data))
        if request.full_url == 'https://api.tavily.com/search':
            assert 0 < timeout <= 5 and request.get_header('Authorization') == 'Bearer fake-test-key'
            assert data == {'query': query, 'search_depth': 'basic', 'max_results': 4,
                            'include_answer': False, 'include_raw_content': False}
            if mode == 'timeout':
                raise TimeoutError()
            return json_response({'results': [{
                'title': '미래솔 별칭', 'content': '미래솔은 미래솔루션의 별칭이다. 도구 전용 문구 ' + '글' * 480,
                'raw_content': '원문 데이터', 'url': 'https://example.test/',
            }] * 5})
        if data.get('tools'):
            assert data['tools'] == [engine.WEB_SEARCH_TOOL] and data['format'] == engine.WEB_LOOKUP_SCHEMA
            assert '사전에 없는 줄임말' in data['messages'][0]['content']
            assert json.loads(data['messages'][1]['content'])['web_candidates'] == ['미래솔']
            if mode in ('no-call', 'structured-tool'):
                content = json.dumps({'query': query if used else None})
                return json_response({'message': {'role': 'assistant', 'content': content}})
            tool_call = {'function': {'name': 'web_search', 'arguments': {
                'query': '러브버그' if mode == 'unrelated-tool' else query}}}
            return json_response({'message': {'role': 'assistant', 'content': '', 'tool_calls': [tool_call] * 2}})
        if used:
            assert data['format'] == engine.ASK_WEB_SCHEMA and 'tools' not in data
            assert len(data['messages'][-2]['tool_calls']) == 1
            tool = data['messages'][-1]
            assert tool['role'] == 'tool' and tool['tool_name'] == 'web_search'
            evidence = json.loads(tool['content'])
            assert len(evidence) == 4 and set(evidence[0]) == {'title', 'summary'}
            assert len(evidence[0]['summary']) == 350
        else:
            assert data['format'] == engine.ASK_SCHEMA and 'tools' not in data
            assert [message['role'] for message in data['messages']] == ['system', 'user']
        return json_response({'message': {'role': 'assistant', 'content': json.dumps(final)}})

    monkeypatch.setattr(engine, 'urlopen', mock_urlopen)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '' if mode == 'no-key' else 'fake-test-key')
    monkeypatch.setattr(engine, 'known_news_words',
                        lambda words: set(words) - (set() if mode == 'known' else {'미래솔'}))
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'postprocess_question',
                        lambda raw: postprocess(raw, lookup=lambda words: {word: 1 for word in words}))
    params = {'mode': 'empty', 'q': '', 'keywords': []}
    with patch.dict(engine._ask_cache, clear=True):
        result = engine.convert_question(question)
        assert result['web_query'] == (query if used else None)
        assert result['q'] == (question + ' 미래솔루션' if used else question)
        assert ('도구 전용 문구' not in json.dumps(result, ensure_ascii=False)
                and '도구 전용 문구' not in repr(engine._ask_cache))
    external_calls = [call for call in calls if call[0] == 'https://api.tavily.com/search']
    assert len(external_calls) == (1 if used or mode == 'timeout' else 0)
    assert len(calls) == (3 if used or mode == 'timeout' else 2 if mode in ('unrelated-tool', 'no-call') else 1)
    html = app.render_page({}, {'conversion': result, 'params': params, 'events': [], 'timings': {},
                                'stats': {'basis': '', 'monthly': []}, 'candidate_count': 0, 'elapsed_ms': 0})
    assert ('웹 확인: 사용(미래솔 → 미래솔루션)' if used else '웹 확인: 미사용') in html
    assert '도구 전용 문구' not in html
    if mode == 'known':
        assert '웹 확인: 미사용(알려진 낱말)' in html


def test_web_definition_is_grounded_and_checked_as_keyword():
    evidence = [{'title': '아보하와 일상', 'summary': '아보하(아주 보통의 하루)가 트렌드다.'}]
    alias = engine.grounded_web_alias(None, ['아보하'], evidence)
    assert alias == {'term': '아보하', 'canonical': '아주 보통의 하루'}
    assert engine.grounded_web_alias({'term': '아보하', 'canonical': '일상생활'}, ['아보하'],
                                     [{'title': '아보하', 'summary': '다른 내용'}]) is None
    raw = {'q': '아보하', 'keywords': ['아보하'], 'resolved_alias': alias}
    result = engine.postprocess_question(raw, lambda _: {'아주보통의하루': 2, '아보하': 1})
    assert result['keywords'] == ['아주보통의하루']
    evidence = [{'title': '신조어', 'summary': "'갓벽'은 '갓(God)'과 '완벽'의 조합이다."}]
    assert engine.grounded_web_alias(None, ['갓벽'], evidence) == {'term': '갓벽', 'canonical': '완벽'}


def test_web_gate_checks_only_unregistered_question_words(monkeypatch):
    def known(words):
        assert set(words) == {'러브버그', '러브버그가', '아보하'}
        return {'러브버그'}

    monkeypatch.setattr(engine, 'known_news_words', known)
    assert engine.unfamiliar_news_words('지난달 동아일보에서 국대 축구, 요즘 러브버그가 많아 아보하 뜻은?') == ['아보하']


def test_web_deadline_discards_late_evidence_and_uses_normal_conversion(monkeypatch):
    tavily_entered, release_tavily, worker_done = Event(), Event(), Event()
    raw = {'keywords': [], 'q': '아보하'}
    tool_call = {'function': {'name': 'web_search', 'arguments': {'query': '아보하'}}}
    model = []

    def local_message(messages, **kwargs):
        model.append(kwargs)
        if kwargs.get('tools'):
            return {'tool_calls': [tool_call]}, 1.0
        return {'content': json.dumps(raw)}, 1.0

    def tavily(*args, **kwargs):
        tavily_entered.set()
        assert release_tavily.wait(3)
        return [{'title': '아보하 별칭', 'summary': '도착이 늦은 결과'}]

    convert = engine._web_query_conversion

    def track_worker(*args):
        try:
            return convert(*args)
        finally:
            worker_done.set()

    monkeypatch.setattr(engine, 'WEB_CONVERSION_TIMEOUT', .2)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: 'fake-test-key')
    monkeypatch.setattr(engine, 'known_news_words', lambda _: set())
    monkeypatch.setattr(engine, '_ollama_message', local_message)
    monkeypatch.setattr(engine, 'tavily_search', tavily)
    monkeypatch.setattr(engine, '_web_query_conversion', track_worker)
    try:
        result = engine.convert_question('아보하')
        assert tavily_entered.is_set() and not worker_done.is_set()
        assert result['q'] == '아보하' and result['web_query'] is None and result['web_reason'] == '시간 초과'
    finally:
        release_tavily.set()
    assert worker_done.wait(3)
    assert len(model) == 2 and model[-1] == {'schema': engine.ASK_SCHEMA}


def test_ungrounded_constraints_removed_and_topic_cleaned():
    raw = {'keywords': ['러브버그'], 'q': '러브버그 대량 발생', 'date_from': '2026-10-01',
           'date_to': None, 'publisher': '동아일보'}
    result = engine.ground_question_filters(raw, '러브버그 대량 발생', date(2026, 11, 18))
    assert result['date_from'] is result['date_to'] is result['publisher'] is None
    assert engine.postprocess_question({'keywords': [], 'q': '주가', 'publisher': 'null'}, lambda _: {})['publisher'] is None
    result = engine.ground_question_filters({**raw, 'q': '2026년 10월 삼성전자 주가',
                                            'keywords': ['삼성전자', '10월']}, '2026년 10월 삼성전자 주가', date(2026, 11, 18))
    assert result['q'] == '삼성전자 주가' and result['keywords'] == ['삼성전자']


def test_shared_parameterized_sql():
    fields = {'date_from': '2026-10-01', 'date_to': '2026-10-31', 'publisher': "동아일보' OR TRUE --"}
    p = engine.validate_params({'keywords': '반도체', 'q': '수출', **fields})
    for sql, values in (engine.candidate_query(p, '[]', [1]), engine.stats_query(['반도체'], params=p),
                        engine.keyword_timeline_query(p, '[]'),
                        engine.candidate_query(engine.validate_params({'q': '수출', **fields}), '[]')):
        assert 'published_date >= %s::date' in sql and 'published_date <= %s::date' in sql
        assert 'article_sources fs' in sql and 'fs.publisher=%s' in sql
        assert fields['publisher'] not in sql and fields['publisher'] in values
        assert sql.count('%s') == len(values)
    sql, _ = engine.candidate_query(engine.validate_params({'q': '수출', **fields}), '[]')
    assert sql.index('fs.publisher=%s') < sql.index('ORDER BY distance LIMIT')
    assert 'eligible AS MATERIALIZED' in sql and 'FROM eligible ORDER BY distance LIMIT' in sql
    assert 'fs.publisher=%s OFFSET 0)' in engine.stats_query(['반도체'], params=p)[0]


def test_ai_failure_keeps_manual_filters():
    params = engine.validate_params({'keywords': '반도체', 'date_from': '2026-10-01', 'publisher': '동아일보'})
    expected = {k: params[k] for k in ('keywords', 'q', 'limit', 'date_from', 'date_to', 'publisher')}
    with patch.object(engine, 'convert_question', side_effect=engine.LLMUnavailable('로컬 AI 연결 안 됨')), patch.object(
            engine, '_search_db', return_value={'events': [], 'timings': {}}), patch.object(
            engine, 'connection_options', return_value={}), patch.object(engine.psycopg, 'connect') as connect:
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {
            'transaction_read_only': 'on', 'present': True,
        }
        result = engine.search({**expected, 'ask': '지난달 반도체 기사'})
    assert result['params'] == params and 'ai_error' in result


def test_conversion_lookup_failure_is_manual_fallback_error():
    with patch.object(engine, 'ollama_chat', return_value=({'keywords': ['반도체'], 'q': '수출'}, 1)), patch.object(
            engine, 'postprocess_question', side_effect=RuntimeError('원시 DB 오류')):
        with pytest.raises(engine.LLMError, match='수동 검색 조건'):
            engine.convert_question('해석 실패 테스트', date(2026, 11, 18))


def test_form_edit_and_timeline_keep_conditions():
    p = engine.validate_params({'keywords': '반도체', 'date_from': '2026-10-01', 'date_to': '2026-10-31', 'publisher': '동아일보'})
    with patch.object(app, 'search', side_effect=lambda params: params):
        sent, status = app.handle_query(urlencode({'kw': '반도체', 'date_from': '2026-10-01', 'date_to': '2026-10-31',
                                                  'publisher': '동아일보'}), as_json=True)
    assert status == 200 and sent['publisher'] == '동아일보' and sent['date_from'] == '2026-10-01'
    html = app.render_page({'date_from': '2026-10-01', 'date_to': '2026-10-31', 'publisher': '<script>'})
    assert 'type="date"' in html and 'value="2026-10-01"' in html and '&lt;script&gt;' in html
    from html import unescape
    import re
    url = re.search(r'data-url="([^"]+)"', app.timeline_placeholder({'params': p}))[1]
    query = parse_qs(urlsplit(unescape(url)).query)
    assert query['date_from'] == ['2026-10-01'] and query['publisher'] == ['동아일보']
