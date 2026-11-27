from datetime import date
from html import escape, unescape
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock

import pytest

from search import app, engine


@pytest.mark.parametrize('question, name', [
    ('기아타이거즈 야구 순위', '기아타이거즈'),
    ('기아 타이거즈 순위', '기아타이거즈'),
    ('기업인 김도영', '김도영'),
    ('정치인 김도영', '김도영'),
    ('삼성 라이온즈 순위', '삼성라이온즈'),
    ('삼성전자 실적', '삼성전자'),
])
def test_recognized_queries_skip_llm(monkeypatch, question, name):
    chat = Mock(side_effect=AssertionError('불필요한 LLM 호출'))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    result = engine.convert_question(question, date(2026, 11, 18))
    assert result['keywords'] == [name] and result['q'] == question and result['llm_ms'] == 0
    chat.assert_not_called()


@pytest.mark.parametrize('topic', ['이도현: 회사 순위', '이도: 기업인', '기업인 이도현 발표', '기업 이도현'])
def test_unverified_topics_fall_back_to_question(topic):
    assert engine.grounded_topic(topic, '기업인 이도현') == '기업인 이도현'
    assert engine.grounded_topic('이도현', '기업인 이도현') == '이도현'


def test_aliases_preserve_intersection_and_disambiguate_company():
    params = engine.validate_params({'keywords': '기아 타이거즈, 야구, LG', 'q': '기아타이거즈 야구 순위'})
    assert params['keywords'] == ['기아타이거즈', 'LG']
    sql, values = engine.keyword_source(params['keywords'], params['q'])
    assert 'UNION' in sql and 'INTERSECT' in sql and 'KIA' in values[0]
    assert '기아' not in values[0] and values[-2] == ['LG', 'lg']
    assert re.search(values[-1], 'LG는 승리했다') and not re.search(values[-1], 'ALGORITHM')
    assert engine.query_entities('KIA 전기차 판매') == []
    assert engine.query_entities('삼성전자 실적') == ['삼성전자']
    for sql, values in (engine.candidate_query(params, '[]', [1]), engine.keyword_timeline_query(params, '[]')):
        assert 'rank_score' in sql and sql.count('%s') == len(values)


def test_named_title_threshold_and_context_adjusted_sort():
    assert engine.passes_result_filter({'score': .8, 'name_match': True}, .85)
    assert not engine.passes_result_filter({'score': .8, 'name_match': True, 'wrong_context': True}, .85)
    assert not engine.passes_result_filter({'score': .8, 'name_match': False}, .85)
    assert engine.passes_result_filter({'score': None, 'name_match': True, 'context_match': True}, .85)
    events = [dict(group_id=1, score=.8, rank_score=1.0, latest_match='2026-01-01', article_count=3),
              dict(group_id=2, score=.9, rank_score=.9, latest_match='2026-01-02', article_count=1)]
    assert engine.sort_result_events(events, 'combined', similarity=True) == events
    assert engine.sort_result_events(events, 'combined') == events[::-1]


@pytest.mark.parametrize(('query', 'title', 'unrelated'), [
    ('AI', '생성형 ai가 바꾼 업무', 'OpenAI와 AIG'),
    ('BTS', 'BTS의 새 음반', 'XBTS2 출시'),
    ('오징어게임', '오징어 게임 시즌 3', '오징어 양식법'),
])
def test_bare_title_terms_use_exact_lookup(monkeypatch, query, title, unrelated):
    chat = Mock(side_effect=AssertionError('불필요한 LLM 호출'))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    result = engine.convert_question(query)
    assert result['keywords'] == [query]
    params = engine.validate_params({'keywords': result['keywords'], 'q': result['q']})
    sql, bindings = engine.keyword_source(params['keywords'], params['q'])
    assert 'UNION SELECT id FROM articles WHERE title ~* %s' in sql
    assert re.search(bindings[-1], title, re.IGNORECASE)
    assert not re.search(bindings[-1], unrelated, re.IGNORECASE)
    ranked_sql, ranked_values = engine.candidate_query(params, '[1]', [1])
    assert 'context_match DESC' in ranked_sql and ranked_sql.count('%s') == len(ranked_values)
    chat.assert_not_called()


def test_scoped_abbreviation_survives_model_omission(monkeypatch):
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: ({'keywords': [], 'q': '뉴스'}, 1.0))
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    result = engine.convert_question('어제 ai 뉴스', date(2026, 11, 20))
    assert (result['keywords'], result['q'], result['date_from'], result['date_to']) == (
        ['ai'], 'ai', '2026-11-19', '2026-11-19')


@pytest.mark.parametrize(('topic', 'keyword', 'focused', 'unrelated'), [
    ('트럼프 관세', '트럼프', '트럼프 관세 논의', '트럼프 취임식 참석'),
    ('국민의힘 전당대회', '국민의힘', '국민의힘 전당 대회에서 대표 선출', '국민의힘 최고위원 회의'),
    ('인공지능 규제', '인공지능', 'AI 규제기관 신설 논의', '인공지능 투자 발표'),
])
def test_compound_topic_prioritizes_added_title_evidence(topic, keyword, focused, unrelated):
    params = engine.validate_params({'keywords': keyword, 'q': topic})
    subject, context, _, _ = engine.topic_title_focus(topic)
    assert re.search(subject, focused, re.IGNORECASE) and re.search(context, focused)
    assert not re.search(context, unrelated)
    sql, values = engine.candidate_query(params, '[1]', [1])
    assert 'context_match DESC' in sql and 'context_match AS aspect_match' in sql
    assert sql.count('%s') == len(values) and context in values
    assert engine.passes_result_filter({'score': .9}, .85)


def test_converted_compound_terms_are_optional_title_evidence(monkeypatch):
    topic = '트럼프 관세 협상'
    raw = {'keywords': topic.split(), 'q': topic}
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1.0))
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, 'postprocess_question', lambda payload: postprocess(
        payload, lookup=lambda words: dict.fromkeys(words, 1)))
    assert engine.convert_question(topic, date(2026, 11, 20))['keywords'] == ['트럼프']
    manual = engine.validate_params({'keywords': topic.split(), 'q': topic})
    assert manual['keywords'] == topic.split()
    generic = engine.validate_params({'keywords': '반도체', 'q': '반도체 수출 기사'})
    assert engine.additional_title_pattern(generic) == '수출'


@pytest.mark.parametrize(('keyword', 'topic', 'extra'), [
    ('SK하이닉스', '실적 SK 하이닉스 SK', '실적'),
    ('SK하이닉스', 'SK하이닉스 실적', '실적'),
    ('LG전자', 'LG 전자 매출', '매출'),
    ('LG전자', 'LG디스플레이 실적', ''),
])
def test_additional_title_weight_ignores_parts_of_required_name(keyword, topic, extra):
    assert engine.additional_title_pattern({'q': topic, 'keywords': [keyword]}) == extra


def test_hynix_earnings_uses_title_evidence_for_the_company():
    conversion = engine.convert_question('SK하이닉스 실적')
    assert conversion['q'] == 'SK하이닉스 실적' and conversion['keywords'] == ['SK하이닉스']
    subject, positive, negative, focus = engine.topic_title_focus('실적 SK 하이닉스')
    for title in ('SK하이닉스 영업익 37.6조', 'SK하이닉스 영업이익 두 배',
                  'SK 하이닉스, 매출 104조', 'SK하닉 2분기 사상 최대'):
        assert re.search(subject, title) and re.search(positive, title)
    assert re.search(focus, '영업이익 매년 두배')
    assert not re.search(positive, 'SK 하이닉스, 직원용 임대아파트 위치 윤곽')
    assert not re.search(positive, 'SK스퀘어, 영업익 7조9406억 하이닉스 현금배당 효과')
    assert re.search(negative, '[기업] SK스퀘어, 영업익 7조9406억')


def test_missing_mixed_script_company_is_restored_as_one_word(monkeypatch):
    raw = {'keywords': [], 'q': '실적', 'publisher': '동아일보'}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1.0))
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    conversion = engine.convert_question('지난달 동아일보 SK하이닉스 실적', date(2026, 11, 20))
    assert conversion['q'] == '실적 SK하이닉스' and conversion['publisher'] is None


@pytest.mark.parametrize('question', ['삼성 주가', '삼성의 주가'])
def test_samsung_price_resolves_to_samsung_electronics(question):
    conversion = engine.convert_question(question)
    assert conversion['keywords'] == ['삼성전자'] and conversion['q'] == '삼성전자 주가'
    saved = engine.validate_params({'keywords': '삼성', 'q': question})
    assert saved['keywords'] == ['삼성전자'] and saved['q'] == '삼성전자 주가'
    for other_topic in ['삼성 실적', '삼성 주가조작', '삼성생명 주가', '삼성 라이온즈 경기']:
        assert engine.standardize_question(other_topic) == other_topic
    assert engine.validate_params({'keywords': '삼성생명', 'q': question})['keywords'] == ['삼성생명']


def test_short_caption_does_not_outrank_report(monkeypatch):
    day = '2026-11-19'
    rows = [
        dict(id=1, group_id=None, score=.94, published_date=day, short_title=True, ranked_candidate_count=2),
        dict(id=2, group_id=None, score=.91, published_date=day, short_title=False, ranked_candidate_count=2),
    ]
    details = [dict(id=row['id'], title=title, published_date=day, publisher='동아일보')
               for row, title in zip(rows, ('반등한 코스피', '외국인 순매수 전환에 코스피 4,000선 회복'))]
    conn = Mock()
    conn.execute.side_effect = [Mock(**{'fetchall.return_value': rows}), Mock(**{'fetchall.return_value': details})]
    monkeypatch.setattr(engine, 'find_core_articles', lambda *_: {1: True, 2: True})
    monkeypatch.setattr(engine, 'collect_core_stats', lambda *_: {})
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: [1, 2])
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    params = engine.validate_params({'keywords': '코스피', 'q': '코스피'})
    result = engine._search_db(conn, params, '[1]')
    assert [event['representative_id'] for event in result['events']] == [2, 1]


def test_digest_defaults_to_major_events_and_supports_latest(monkeypatch):
    params = engine.validate_params({'q': '뉴스', 'date_from': '2026-11-19', 'date_to': '2026-11-20'})
    events = [{'latest_match': '2026-11-19'}, {'latest_match': '2026-11-20'}]
    result = {'params': params, **engine.empty_search_result('기간'), 'events': events}
    monkeypatch.setattr(app, 'search', lambda *_, **__: result)
    query = urlencode({'q': '뉴스', 'date_from': '2026-11-19', 'date_to': '2026-11-20'})
    assert app.handle_query(query, as_json=True)[0]['events'] == events
    assert app.handle_query(query + '&sort=latest', as_json=True)[0]['events'] == events[::-1]


def test_shared_search_uses_saved_conditions(monkeypatch):
    search = Mock(return_value={})
    monkeypatch.setattr(app, 'search', search)
    query = dict(ask='기아 타이거즈 순위', kw='KIA', q='순위', date_from='2026-01-01',
                 date_to='2026-03-31', publisher='동아일보', sort='latest', _resolved=True)
    url = app.result_url(query)
    assert parse_qs(urlsplit(url).query)['ask'] == [query['ask']]
    assert app.handle_query(urlsplit(url).query, as_json=True)[1] == 200
    expected = {key: query[key] for key in ('q', 'date_from', 'date_to', 'publisher')}
    search.assert_called_once_with({'keywords': 'KIA', **expected}, sort='latest')


def test_full_reports_followups_and_csv_metadata():
    def report(ident, title, day, publisher='동아일보'):
        return dict(id=ident, title=title, published_date=day, publisher=publisher,
                    url='https://example.com/news', categories=[], score=.82, body='기사 본문')

    reports = [report(i, '제목이 같은 기사', '2026-01-01') for i in range(1, 7)]
    item = dict(group_id=1, grouped=True, representative=reports[0], date_label='2026-01-01',
                date_from='2026-01-01', date_to='2026-01-01', article_count=6,
                publishers=['동아일보', '신문사'], score=.82, matches=[reports[0]])
    timeline = dict(items=[item], months=[], llm_ms=0, model='test')
    followups = {engine.followup_key(item): [report(7, '동아일보 후속 기사', '2026-01-02')],
                 (2, '2026-01-01'): [report(8, '다른 사건의 후속 기사', '2026-01-02', '신문사')]}
    panel = app.render_timeline(timeline, [], {(True, 1): reports}, followups)
    report_lists = re.findall(r'<ol class="article-list">(.*?)</ol>', panel, re.S)
    assert '관련 보도 6건' in panel and [rows.count('<li>') for rows in report_lists] == [6, 1]
    assert '동아일보 외 1개 언론사' in panel
    assert '동일 보도 보기' in panel and '후속 기사 보기' in panel and '동아일보 후속 기사' in panel
    assert '다른 사건의 후속 기사' not in panel and '다른 언론사 보도' not in panel
    assert '이 기간(종료 후 60일) 동아일보 후속 보도 없음' in app.render_timeline(
        timeline, [], {(True, 1): reports})
    card = app.render_card(dict(item, articles=reports), 1)
    assert card.count('data-csv=') == 6 and 'data-score="0.8200"' in card
    assert 'title="AI가 산정한 참고용 유사도' in card and '기사 본문' not in card + panel


def test_followup_gap_and_other_publishers_use_each_event_end_date():
    rep = dict(id=1, title='김도영 기사', publisher='동아일보')
    second = dict(id=2, title='김도영 다른 사건', publisher='동아일보')
    members = [dict(group_id=1, grouped=True, date_to='2026-06-07', representative=rep),
               dict(group_id=2, grouped=False, date_to='2026-06-09', representative=second)]
    item = dict(members[0], date_from='2026-06-07', date_to='2026-06-09', date_label='2026-06-07',
                publishers=['동아일보'], sub_events=members, event_count=2)
    followups = {
        engine.followup_key(item): [
            dict(title='6월 후속', published_date='2026-06-23', publisher='다른신문'),
            dict(title='7월 후속', published_date='2026-07-10', publisher='다른신문'),
        ],
        engine.followup_key(members[0]): [dict(title='하위 사건 후속', published_date='2026-06-20', publisher='동아일보')],
    }
    panel = app.render_timeline(dict(items=[item], months=[], llm_ms=0, model='test'), [], {}, followups)
    assert '동아일보 후속 보도 없음' not in panel
    assert panel.count('하위 사건 후속') == 1
    assert panel.index('하위 사건 후속') < panel.index('6월 후속')
    assert panel.index('6월 후속') < panel.index('다음 보도까지 17일') < panel.index('7월 후속')
    assert '다음 보도까지 13일' not in panel
    without_donga = app.render_timeline(dict(items=[item], months=[], llm_ms=0, model='test'), [], {},
                                        {engine.followup_key(item): followups[engine.followup_key(item)]})
    assert without_donga.count('동아일보 후속 보도 없음') == 1


def test_followup_targets_prefer_player_over_team_and_preserve_search_rules():
    params = engine.validate_params({'keywords': '기아타이거즈', 'q': '기아타이거즈 야구 순위'})
    targets, wrong = engine.followup_targets(
        'KIA 김도영 멀티포 폭발, 시즌 18호 오스틴 제치고 단독 선두', ['KIA', '선두', '김도영', '오스틴'], params)
    assert targets == ['김도영']
    assert wrong == engine.AUTO_CONTEXT
    params = engine.validate_params({'keywords': '삼성전자', 'q': '삼성전자 주가'})
    assert engine.followup_targets('삼성 전자 장중 급락', [], params) == (['삼성전자'], '')
    params = engine.validate_params({'keywords': '김도영'})
    assert engine.followup_targets('카카오, ‘AI’-‘미래 투자’로 나눈다',
                                   ['카카오', 'AI', '투자', '대표', '미래'], params) == (['카카오', 'AI', '투자'], '')


def test_team_timeline_filters_candidates_and_group_representatives():
    params = engine.validate_params({'keywords': 'KIA'})
    rule, args = engine.timeline_context_filter(params)
    assert re.search(args[0], '[오늘의 날씨] 2026년 3월 26일')
    assert re.search(args[1].replace(r'\m', r'\b').replace(r'\M', r'\b'), 'KIA 전기차 판매')
    assert re.search(args[2], 'KIA 김도영 홈런')
    sql, values = engine.keyword_timeline_query(params, None)
    assert sql.count(rule) == 2 and values[-9:-5] == args and values[-5:-1] == args
    assert sql.count('%s') == len(values) and 'ORDER BY subject_headlines DESC, publisher_count DESC, article_count DESC' in sql
    assert re.search(values[0], 'KIA 김도영 홈런') and re.search(values[0], '기아 타이거즈 승리')
    assert not re.search(values[0], '류현진, 한미통산 200승 축포')


def test_timeline_prefers_subject_headlines_and_only_fills_unused_slots():
    indirect = dict(group_id=1, article_count=20, subject_headlines=False, relevance=2, score=.95,
                    date_from='2026-05-24', date_to='2026-05-25')
    direct = dict(indirect, group_id=2, article_count=3, subject_headlines=True, relevance=1, score=.85)
    result = dict(events=[indirect, direct], params={'q': 'KIA 야구'})
    assert engine.select_timeline_events(result, result['events'], limit=1) == [direct]
    assert engine.select_timeline_events(result, result['events'], limit=2) == [indirect, direct]
    assert engine.timeline_subject_pattern({'keywords': ['김도영']})
    assert not engine.timeline_subject_pattern({'keywords': ['삼성전자', '러브버그']})


def test_followup_lookup_batches_representatives_and_keeps_distinct_end_dates():
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': [{'article_id': 1, 'keywords': ['김도영']},
                                         {'article_id': 2, 'keywords': ['이의리']}]}),
        Mock(**{'fetchall.return_value': [
            dict(representative_id=1, end_date=date(2026, 6, 9), id=4, title='KIA 김도영',
                 published_date=date(2026, 6, 10)),
            dict(representative_id=2, end_date=date(2026, 6, 10), id=5, title='KIA 이의리',
                 published_date=date(2026, 6, 11)),
        ]}),
    ]
    first = dict(representative=dict(id=1, title='KIA 김도영 멀티포'), date_to='2026-06-07')
    second = dict(representative=dict(id=2, title='KIA 이의리 복귀'), date_to='2026-06-10')
    params = engine.validate_params({'keywords': '기아타이거즈', 'q': '기아타이거즈 야구',
                                     'date_to': '2026-06-09', 'publisher': '동아일보'})
    params['excluded_categories'] = engine.excluded_categories(params['q'])
    results = engine.event_followups(conn, [first, second, dict(first, date_to='2026-06-09'), first], params)
    assert conn.execute.call_count == 2
    _, (ids,) = conn.execute.call_args_list[0].args
    sql, (window, payload, publisher, *limits) = conn.execute.call_args.args
    assert ids == [1, 2]
    assert [(r['article_id'], r['end_date'], r['terms'], r['patterns']) for r in json.loads(payload)] == [
        (1, '2026-06-07', ['%김도영%'], []), (2, '2026-06-10', ['%이의리%'], []),
        (1, '2026-06-09', ['%김도영%'], [])]
    condition, context_values = engine.timeline_context_filter(params)
    assert context_values[0] == ['경제'] and sql.count(condition) == 2
    assert window == 60 and publisher == '동아일보'
    assert limits == [*context_values, .85, 10, *context_values, '동아일보', .85, 5]
    assert sql.count('%s') == 3 + len(limits)
    assert 'published_date > w.end_date' in sql and 'published_date <= w.last_date' in sql
    assert 'AS evidence_keywords FROM followups later' in sql
    assert results[(1, '2026-06-09')][0]['published_date'] == '2026-06-10'
    assert results[(2, '2026-06-10')][0]['id'] == 5


def test_result_refinement_share_keeps_base_search_and_escapes_controls(monkeypatch):
    article = dict(id=1, title='반도체 수출', published_date='2026-11-12', publisher='동아일보', categories=[])
    event = dict(representative=article, matches=[article], articles=[dict(article, id=2, publisher='한겨레')],
                 score=.90, date_from='2026-11-12', date_to='2026-11-12')
    params = dict(mode='topic', keywords=[], q='아보하 아주 보통의 하루')
    result = dict(events=[event], params=params, candidate_count=1, elapsed_ms=1, result_min_cos=.85,
                  timings={}, stats=dict(monthly=[], basis=''))
    search = Mock(return_value=result)
    monkeypatch.setattr(app, 'search', search)
    monkeypatch.setattr(app, 'ollama_available', lambda: True)
    monkeypatch.setattr(app, 'list_publishers', lambda: [
        {'publisher': '동아일보', 'article_count': 2000}, {'publisher': '한겨레', 'article_count': 1000}])
    fields = dict(ask='아보하 관련 기사', q=params['q'], resolved='1', web_term='아보하', web_name='아주 보통의 하루',
                  within='반도체', without='"<script>', within_from='2026-11-01', within_to='2026-11-19',
                  publisher='한겨레', sort='latest')
    html, status = app.handle_query(urlencode(fields))
    assert status == 200 and '결과 내 검색' in html
    search.assert_called_once_with({'keywords': '', 'q': params['q'], 'publisher': '한겨레'},
                                   sort='latest', web_alias={'term': '아보하', 'canonical': '아주 보통의 하루'})
    assert 'value="&quot;&lt;script&gt;"' in html and 'value="한겨레" selected' in html
    assert 'data-representative="1"' in html and html.count('data-csv=') == 2
    url = app.result_url({**fields, '_resolved': True, '_web_alias': search.call_args.kwargs['web_alias']})
    assert parse_qs(urlsplit(url).query) == {key: [value] for key, value in fields.items()}


@pytest.mark.parametrize(('params', 'present', 'expected'), [
    ({'q': '코스피', 'publisher': '동아일보', 'date_from': '2026-11-09'}, [False], 'scope_empty'),
    ({'q': '트럼프 관세 협상', 'publisher': '연합뉴스'}, [True], None),
    ({'q': '트럼프 관세 협상'}, [], None),
])
def test_article_coverage_checks_only_scope(params, present, expected):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [{'present': value} for value in present]
    params = engine.validate_params(params)
    assert engine.missing_article_coverage(conn, params) == expected
    assert conn.execute.call_count == len(present)
    for call in conn.execute.call_args_list:
        sql, values = call.args
        assert sql.count('%s') == len(values) and 'SELECT EXISTS' in sql
    if expected:
        html = app.render_page({}, {'params': params, **engine.empty_search_result('범위'),
                                    'elapsed_ms': 0, 'data_absence': expected})
        assert app.DATA_ABSENCE_MESSAGES[expected] in html and 'id="timeline-slot"' not in html


@pytest.mark.parametrize(('topic', 'keywords', 'headline', 'unrelated'), [
    ('이재명 재판 결과', '이재명', '이재명 대통령 재판 중단', '이재명 캠프 인사들 1심 무죄'),
    ('월드컵 한국 경기 일정', '월드컵', '홍명보호 월드컵 첫 경기 일정', '여자 농구 대표팀 월드컵 경기'),
])
def test_focused_headlines_keep_specific_events_above_semantic_cutoff(topic, keywords, headline, unrelated):
    params = engine.validate_params({'q': topic, 'keywords': keywords})
    patterns = engine.title_signals(params)
    sql, values = engine.candidate_query(params, '[]', [1])
    assert sql.count('%s') == len(values) and 'rank_score' in sql
    for title, expected in ((headline, True), (unrelated, False)):
        row = {key: bool(re.search(pattern, title)) for key, pattern in
               zip(('name_match', 'context_match', 'wrong_context', 'focus_match'), patterns)}
        assert engine.passes_result_filter({**row, 'score': .70}, .85, use_title_context=True) is expected


def test_latest_api_order_keeps_same_results(monkeypatch):
    events = [{'group_id': 1, 'latest_match': '2026-01-01'}, {'group_id': 2, 'latest_match': '2026-01-02'}]
    search = Mock(return_value={'events': events})
    monkeypatch.setattr(app, 'search', search)
    assert app.handle_query(urlencode({'q': '주제'}), as_json=True)[0]['events'] == events
    assert app.handle_query(urlencode({'q': '주제', 'sort': 'latest'}), as_json=True)[0]['events'] == events[::-1]
    assert search.call_args_list[0].args == search.call_args_list[1].args
    assert search.call_args_list[1].kwargs == {'sort': 'latest'}


def test_bare_registered_person_uses_required_keyword_without_topic(monkeypatch):
    chat = Mock(side_effect=AssertionError('불필요한 LLM 호출'))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    assert engine.convert_question('김도영')['keywords'] == ['김도영']
    assert engine.convert_question('김도영')['q'] == ''
    assert engine.validate_params({'q': '김도영'}) == engine.validate_params({'keywords': '김도영'})
    assert engine.validate_params({'q': '야구선수 김도영'})['keywords'] == ['김도영']
    chat.assert_not_called()


@pytest.mark.parametrize(('counts', 'expected'), [([20, 10], 2), ([20, 1], 0)])
def test_person_branches_use_scoped_search_and_require_two_meaningful_groups(monkeypatch, counts, expected):
    search = Mock(side_effect=[{'event_count': count} for count in counts])
    monkeypatch.setattr(engine, '_search_db', search)
    monkeypatch.setattr(engine, 'query_embedding', lambda topic: topic)
    params = engine.validate_params({'keywords': '김도영', 'publisher': '동아일보', 'date_from': '2026-05-01'})
    conn = Mock()
    branches = engine.person_result_branches(conn, params)
    assert len(branches) == expected
    for call, context in zip(search.call_args_list, ('야구선수', '기업인')):
        db, scoped, vector = call.args
        assert db is conn and scoped == {
            **params, 'mode': 'combined', 'q': f'{context} 김도영',
            'excluded_categories': ['경제'] if context == '야구선수' else ['스포츠'],
        }
        assert vector == scoped['q'] and call.kwargs == {'count_only': True}


def test_person_branch_links_preserve_share_conditions_and_omit_generic_keywords():
    params = engine.validate_params({'keywords': '김도영', 'date_from': '2026-05-01', 'publisher': '동아일보'})
    branches = [{'topic': '야구선수 김도영', 'count': 20}, {'topic': '기업인 김도영', 'count': 10}]
    values = {'ask': '김도영', 'sort': 'latest', 'within': '대표', '_resolved': True}
    nav = app.render_person_branches({'params': params, 'person_branches': branches}, values)
    assert '야구선수 김도영 20건' in nav and '기업인 김도영 10건' in nav and 'aria-current="page">전체' in nav
    links = [parse_qs(urlsplit(unescape(url)).query) for url in re.findall(r'href="([^"]+)"', nav)]
    common = {'kw': ['김도영'], 'sort': ['latest'], 'date_from': ['2026-05-01'],
              'publisher': ['동아일보'], 'within': ['대표']}
    assert links == [{**common, 'q': [branch['topic']]} for branch in branches] + [common]
    assert '전체' in app.render_person_branches({'params': dict(params, q='기업인 김도영')}, values)
    assert not app.render_person_branches({'params': params}, values)
    for term in ('삼성전자', '러브버그'):
        assert not app.render_person_branches({'params': dict(params, keywords=[term])}, values)


@pytest.mark.parametrize('topic, excluded', [
    ('기업인 김도영', ['스포츠']), ('김도영 CEO', ['스포츠']), ('가수 김도영', ['스포츠']),
    ('기아타이거즈 야구 순위', ['경제']), ('삼성전자', []), ('변호사 김도영', []),
    ('의원실 소식', []), ('기업인 대 야구 선수', []),
])
def test_fixed_context_exclusions(topic, excluded):
    assert engine.excluded_categories(topic) == excluded


@pytest.mark.parametrize(('topic', 'keywords', 'sport'), [
    ('국대 축구 불화설', [], '축구'), ('야구 국가대표팀', [], '야구'),
    ('농구 대표팀의 소식', [], '농구'), ('배구 선수 명단', ['대표팀'], '배구'),
])
def test_national_team_context_partitions_candidates_and_filters_stats(topic, keywords, sport):
    params = engine.validate_params({'q': topic, 'keywords': keywords})
    assert params['keywords'] == ['국가대표']
    assert engine.national_team_sports(topic, keywords) == [sport]
    assert engine.national_team_sports('국가대표 AI 애국가') == []
    condition, (other_sports, own_sport, nonsport) = engine.required_title_flag(params)
    assert own_sport == engine.SPORT_TITLE_PATTERNS[sport]
    assert own_sport not in other_sports and all(
        pattern in other_sports for key, pattern in engine.SPORT_TITLE_PATTERNS.items() if key != sport)
    assert "a.title !~* %s AND ((a.title ~* %s AND a.title !~* %s) OR EXISTS" in condition
    assert "='스포츠'" in condition
    candidates, values = engine.candidate_query(params, '[]', [1, 2])
    assert condition in candidates and 'PARTITION BY person_context_match' in candidates
    assert (other_sports, own_sport) in tuple(zip(values, values[1:]))
    stats, values = engine.stats_query(params['keywords'], params=params)
    assert condition in stats and 'ARRAY(SELECT article_id FROM dated)' in stats
    _, source_values = engine.keyword_source(params['keywords'], topic)
    assert stats.count('FROM context_matched') == 2
    assert values[len(source_values):][:3] == (other_sports, own_sport, nonsport)
    timeline, values = engine.keyword_timeline_query(params, '[]')
    assert timeline.count(condition) == 2 and timeline.count('%s') == len(values)


def test_qwen_context_uses_only_fixed_categories_and_penalizes(monkeypatch):
    chat = Mock(return_value=({'keywords': [], 'q': '변호사 김도영', 'person_names': ['김도영'],
                              'person_category': '사회', 'required_terms': ['김도영'],
                              'supporting_terms': ['변호사']}, 24.0))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', engine.OrderedDict())
    monkeypatch.setattr(engine, '_category_cache', engine.OrderedDict())
    result = engine.convert_question('변호사 김도영')
    assert result['keywords'] == ['김도영'] and result['target_category'] == '사회'
    assert result['q'] == '변호사 김도영' and result['llm_ms'] == 24.0
    assert chat.call_count == 1
    assert chat.call_args.args[2]['properties']['person_category']['enum'] == [None, *engine.MAJOR_CATEGORIES]
    chat.return_value = ({'name': '김도영', 'category': '법률'}, 24.0)
    assert engine.infer_person_context('노무사 김도영') is None
    chat.return_value = ({'name': '외부이름', 'category': '사회'}, 24.0)
    assert engine.infer_person_context('세무사 김도영') is None
    chat.side_effect = engine.LLMUnavailable('로컬 AI 연결 안 됨')
    assert engine.infer_person_context('회계사 김도영') is None

    params = engine.validate_params({'keywords': '김도영', 'q': result['q']})
    sql, values = engine.candidate_query({**params, 'target_category': result['target_category']}, '[]', [1, 2])
    assert 'PARTITION BY category_excluded' not in sql
    assert 'score - 0.12 * category_mismatch::int' in sql and '<> ALL' in sql
    assert ['사회', '미분류'] in values and sql.count('%s') == len(values)

    params['target_category'] = result['target_category']
    sql, values = engine.keyword_timeline_query(params, '[]')
    assert 'score - 0.12 * category_mismatch::int' in sql
    assert 'ORDER BY rank_score DESC NULLS LAST' in sql and sql.count('%s') == len(values)
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = []
    event = dict(grouped=False, group_id=1, date_to='2026-06-07', representative=dict(id=1, title='변호사 김도영'))
    engine.event_articles(conn, [event], params, '[]', timeline=True)
    sql, values = conn.execute.call_args.args
    assert ' - 0.12 * ' in sql and values[-1] == ['사회', '미분류'] and sql.count('%s') == len(values)
    engine.event_followups(conn, [event], params)
    sql, values = conn.execute.call_args.args
    assert sql.count('scored.score - 0.12 * ') == 2 and values.count(['사회', '미분류']) == 2
    assert sql.count('%s') == len(values)


def test_timeline_fragment_uses_resolved_context_for_relevance_flow_and_reports(monkeypatch):
    topic = '기업인 김도영'
    params = engine.validate_params({'q': topic})
    rep = dict(id=1, title='제주개발공사 김도영 사장 지명', publisher='동아일보', url='')
    event = dict(group_id=1, grouped=True, date_from='2026-03-01', date_to='2026-03-01',
                 representative=rep, matches=[rep], publishers=['동아일보'], article_count=1,
                 summary='맥락 밖 묶음 요약: 김도영 시즌 첫 홈런')
    result = dict(params=params, excluded_categories=['스포츠'], events=[event])
    monkeypatch.setattr(app, 'handle_query', Mock(return_value=(result, 200)))
    lookup = Mock(return_value=[event])
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', lookup)
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda _: {(True, 1): [1., 0.]})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1,0]')
    monkeypatch.setattr(engine, 'timeline_centers', lambda *_: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    monkeypatch.setattr(engine, '_timeline_cache', engine.OrderedDict())
    chat = Mock(side_effect=[({'1': 2}, 1), ({'months': [], 'summary': ''}, 1)])
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    reports = Mock(return_value=({}, {}))
    monkeypatch.setattr(app, 'timeline_articles', reports)

    html, status = app.handle_timeline(urlencode({'kw': '김도영', 'q': topic, 'fragment': '1'}))
    assert status == 200 and '<h2>기업인 김도영 · 2026.03 ~ 2026.03</h2>' in html
    assert rep['title'] in html and '홈런' not in html
    scoped = {**params, 'excluded_categories': ['스포츠']}
    assert lookup.call_args.args == (scoped,) and reports.call_args.args[1] == scoped
    assert chat.call_count == 2
    relevance, flow = [call.args[1] for call in chat.call_args_list]
    assert topic in relevance and flow['question'] == topic and '홈런' not in str((relevance, flow))
    condition, context_values = engine.context_filter(scoped)
    sql, values = engine.keyword_timeline_query(scoped, '[1,0]')
    assert sql.count(condition) == 2 and values[-2 - 2 * len(context_values):-2] == context_values * 2
    lookup.return_value = []
    html, status = app.handle_timeline(urlencode({'ask': topic, 'fragment': '1'}))
    assert status == 200 and '기업인 김도영 관련 사건이 없습니다' in html


def test_category_exclusion_does_not_pad_or_reintroduce_related_reports(monkeypatch):
    params = dict(engine.validate_params({'q': '기업인 김도영', 'publisher': '동아일보'}),
                  excluded_categories=['스포츠'])
    monkeypatch.setattr(engine, 'find_core_articles', lambda *_: {1: True, 4: True})
    monkeypatch.setattr(engine, 'collect_core_stats', lambda *_: {'basis': '핵심 결과', 'monthly': []})
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: [1, 2, 3, 4, 5])
    monkeypatch.setattr(engine, 'result_min_cos', lambda: .85)

    def candidate(ident, score, **flags):
        return dict(id=ident, group_id=10, published_date=f'2026-01-0{ident}', score=score,
                    rank_score=score, ranked_candidate_count=5) | flags

    def report(row, title, categories):
        return dict(id=row['id'], published_date=row['published_date'], title=title, categories=categories,
                    publisher='동아일보', url='https://example.com/news')

    mixed = candidate(1, .95, person_context_match=True)
    sport = candidate(2, .96, category_excluded=True)
    weak = candidate(3, .70)
    unclassified = candidate(4, .92, category_unclassified=True, person_context_match=True)
    misleading = candidate(5, .91, wrong_context=True, rank_score=.61)
    details = [report(mixed, '기업인 소식', ['경제', '스포츠']), report(sport, '야구 소식', ['스포츠']),
               report(unclassified, '분류 없이 일치한 기사', ['미분류'])]
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': [mixed, sport, weak, unclassified, misleading]}),
        Mock(**{'fetchall.return_value': [
            dict(group_id=10, article_count=2, date_from=mixed['published_date'],
                 date_to=unclassified['published_date'], representative_id=2, publishers=['동아일보'])]}),
        Mock(**{'fetchall.return_value': details}),
        Mock(**{'fetchall.return_value': [dict(details[i], grouped=True, group_id=10) for i in (0, 2)]}),
    ]
    result = engine._search_db(conn, params, '[]')
    event, = result['events']
    assert event['representative']['id'] == 1 and event['latest_match'] == unclassified['published_date']
    assert {article['id'] for article in event['articles']} == {1, 4}
    assert [article['id'] for article in result['excluded_articles']] == [2]
    assert result['excluded_candidate_count'] == 2 and not result['result_filter_relaxed']
    for call in conn.execute.call_args_list:
        sql, values = call.args
        assert sql.count('%s') == len(values)
    sql, values = conn.execute.call_args_list[0].args
    assert 'PARTITION BY category_excluded' in sql and 'bool_and' in sql and ['스포츠'] in values
    assert "btrim(c.major)" in sql and "WHEN 'IT_과학' THEN 'IT·과학'" in sql
    for index in (1, 3):
        assert 'AND NOT COALESCE' in conn.execute.call_args_list[index].args[0]

    html = app.render_page({'ask': '기업인 김도영', 'sort': 'latest'}, {
        'params': params, 'elapsed_ms': 0, **result,
    })
    before, after = html.split('<details id="other-results" class="panel"><summary>다른 김도영 기사 1건 (스포츠)</summary>')
    assert '기업인 김도영 관련 기사 1건' in before and '야구 소식' not in before
    assert 'data-latest="2026-01-04"' in before and '2026-01-01 · 최신 일치 2026-01-04' in before
    assert '야구 소식' in after


@pytest.mark.parametrize('topic, categories, has_title', [
    ('기업인 김도영', ['스포츠'], True), ('경영인 김도영', ['스포츠'], True),
    ('정치인 김도영', ['스포츠'], True), ('가수 김도영', ['스포츠'], False),
    ('삼성전자', [], False), ('기아타이거즈 야구 순위', [], False),
])
def test_stats_scope_preserves_search_ids_and_shares_context_sql(topic, categories, has_title):
    params = dict(engine.validate_params({'q': topic, 'date_from': '2026-02-01', 'publisher': '동아일보'}),
                  excluded_categories=engine.excluded_categories(topic))
    conn = Mock()
    months = [dict(month='2026-02', count=0, top_keywords=[])]
    conn.execute.return_value.fetchone.return_value = dict(monthly=months, article_ids=[1, 2])
    stats, ids = engine.collect_stats(conn, params)
    sql, values = conn.execute.call_args.args
    assert ids == [1, 2] and 'ARRAY(SELECT article_id FROM dated)' in sql
    assert sql.count('%s') == len(values) and ('2026-02-01', '동아일보') == values[2:4]
    assert stats['monthly'] == ([] if categories else months)
    assert ('직함 단서' in stats['basis']) == has_title
    if categories:
        condition, context_values = engine.context_filter({**params, 'excluded_categories': categories})
        category_sql, _ = engine.category_filter(params)
        assert category_sql in condition and condition in sql and context_values[0] == categories
        assert values[4:-1] == context_values and sql.count('FROM context_matched') == 2
        assert f'맥락({topic.split()[0]}) 적용 · 스포츠 분류 제외' in stats['basis']
    elif topic.startswith('기아타이거즈'):
        assert stats['aspects'] == ['야구', '순위']
        assert sql.count('FROM context_matched') == 2 and '맥락(' not in stats['basis']
    else:
        assert 'context_matched' not in sql and '맥락(' not in stats['basis']


@pytest.mark.parametrize('topic, keyword, aspects, samples', [
    ('SK하이닉스 실적', 'SK하이닉스', ['실적'],
     [('3분기 영업익', True), ('매출 발표', True), ('작년 연봉 공개', False), ('성과급 분기 지급', False)]),
    ('삼성전자 주가', '삼성전자', ['주가'],
     [('코스피 급락', True), ('주식 순매수', True), ('주식회사 출범', False)]),
    ('현대차 미국 관세', '현대차', ['미국', '관세'],
     [('미국의 관세를 피했다', True), ('미국 신규 공장', False), ('관세율 인상', False)]),
    ('손흥민 이적', '손흥민', ['이적'], [('임대이적했다', True), ('선발 출전', False)]),
])
def test_monthly_stats_applies_aspects_to_counts_and_keyword_baseline(topic, keyword, aspects, samples):
    params = engine.validate_params({'q': topic, 'keywords': [keyword], 'date_from': '2026-01-01'})
    filters = engine.stats_aspects(params)
    assert list(filters) == aspects
    for title, expected in samples:
        assert all(re.search(pattern, title, re.IGNORECASE) for pattern in filters.values()) == expected

    conn = Mock()
    months = [dict(month='2026-01', count=0, top_keywords=[])]
    conn.execute.return_value.fetchone.return_value = dict(monthly=months, article_ids=[1, 2])
    stats, ids = engine.collect_stats(conn, params)
    sql, values = conn.execute.call_args.args
    assert conn.execute.call_count == 1 and sql.count('%s') == len(values)
    assert ids == [1, 2] and 'ARRAY(SELECT article_id FROM dated)' in sql
    assert stats['aspects'] == aspects and stats['monthly'] == months
    assert '각 관점이 제목 또는 제공 키워드에 일치' in stats['basis']
    assert '주제(q)' not in stats['basis']
    assert sql.count('a.title ~* %s OR EXISTS') == sql.count('ak.keyword ~* %s') == len(aspects)
    assert sql.count('OFFSET 0\n            ))') == len(aspects)
    assert 'FROM context_matched GROUP BY month' in sql
    assert 'FROM context_matched d CROSS JOIN LATERAL' in sql
    assert 'FROM keyword_sums' in sql and 'sum(mention_sum) OVER (PARTITION BY keyword)' in sql
    for word, pattern in filters.items():
        if word == '실적':
            pattern = engine.EARNINGS_ASPECT_CUES
        assert values.count(pattern) == 2
    if '실적' in aspects:
        assert 'a.title !~* %s' in sql and 'ak.keyword !~* %s' in sql
        assert values.count(engine.EARNINGS_ADJACENT) == 2
    assert set(aspects).issubset(values[-1])
    assert f'관점 적용 ({" · ".join(aspects)})' in app.render_stats(stats)


@pytest.mark.parametrize('topic, keyword, category', [
    ('요즘 러브버그 관련 뉴스', '러브버그', None),
    ('기업인 김도영', '김도영', None),
    ('변호사 김도영', '김도영', '사회'),
])
def test_monthly_stats_omits_fillers_and_person_roles(topic, keyword, category):
    params = dict(engine.validate_params({'q': topic, 'keywords': [keyword]}), target_category=category)
    assert not engine.stats_aspects(params)
    sql, _ = engine.stats_query(params['keywords'], params=params)
    assert 'ak.keyword ~* %s' not in sql
    assert '관점 적용' not in app.render_stats(dict(basis='필수 키워드', monthly=[]))


@pytest.mark.parametrize('title, matches', [
    ('김도영 의원, 예산안 발의', True), ('전 장관 김도영, 총선 출마', True),
    ('민주당 후보 김도영, 선거 운동 시작', True), ('김도영 도지사, 정책 발표', True),
    ('김도영 시장, 시정 브리핑', True), ('김도영 서울시장이 발표했다', True),
    ('민주당 김도영, 정책 간담회 참석', True), ('박지원 의원 “KIA 김도영이 해냈다”', False),
    ('제주개발공사 김도영 신임 시장 취임', False),
    ('김도영 前 상무, 제주개발공사 사장 후보 지명', False),
    ('카카오 CA협의체 공동의장에 김도영', False), ('김도영 감독 시장 진출', False),
    ('김도영 시장조사 부문 신임 이사', False),
])
def test_political_title_evidence(title, matches):
    params = dict(engine.validate_params({'q': '정치인 김도영'}), excluded_categories=['스포츠'])
    _, (pattern,) = engine.person_title_flag(params)
    assert bool(re.search(pattern, title)) == matches


@pytest.mark.parametrize('match_exists', [False, True])
def test_political_results_only_count_context_matches(monkeypatch, match_exists):
    params = dict(engine.validate_params({'q': '정치인 김도영'}), excluded_categories=['스포츠'])
    titles = ['김도영 의원, 예산안 발의', '김도영 CTS기독교TV 사장', 'KIA 김도영, 홈런']
    details = [dict(id=i, title=title, publisher='동아일보', published_date='2026-01-01',
                    categories=[], url='https://example.com/news') for i, title in enumerate(titles, 1)]
    months = [dict(month='2026-01', count=1, top_keywords=[])] if match_exists else []
    stats = dict(basis='맥락(정치인) 적용 · 스포츠 분류 제외', monthly=months)
    monkeypatch.setattr(engine, 'find_core_articles', lambda *_: {1: True} if match_exists else {})
    monkeypatch.setattr(engine, 'collect_core_stats', lambda *_: stats)
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: [1, 2, 3])
    monkeypatch.setattr(engine, 'result_min_cos', lambda: .85)
    candidates = [dict(id=i, group_id=None, published_date='2026-01-01', rank_score=.9,
                       ranked_candidate_count=3, score=None if i == 1 else .9,
                       category_excluded=i == 3, person_context_match=i == 1) for i in (1, 2, 3)]
    visible_candidates = candidates if match_exists else candidates[1:]
    monkeypatch.setattr(engine, 'related_article_ids', lambda *_: [])
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': visible_candidates}),
        Mock(**{'fetchall.return_value': details if match_exists else details[1:]}),
        Mock(**{'fetchall.return_value': [dict(details[0], grouped=False, group_id=1)]}),
    ]
    result = engine._search_db(conn, params, '[]')
    assert [e['representative']['id'] for e in result['events']] == ([1] if match_exists else [])
    assert [a['id'] for a in result['other_articles']] == [2]
    assert [a['id'] for a in result['excluded_articles']] == [3]
    sql, values = conn.execute.call_args_list[0].args
    assert 'NOT category_excluded AND person_context_match' in sql and sql.count('%s') == len(values)
    page = app.render_page({'ask': params['q']}, {'params': params, 'elapsed_ms': 0, **result})
    before, after = page.split('<summary>다른 김도영 기사 2건</summary>')
    assert ('정치인 김도영 관련 기사 1건' in before) == match_exists
    assert ('정치인 김도영 기사를 찾지 못했습니다' in before) != match_exists
    assert ('정치인 김도영 관련 사건이 없습니다' in before) != match_exists
    assert ('집계할 기사가 없습니다.' in before) != match_exists
    assert ('id="timeline-slot"' in before) == match_exists
    assert titles[1] in after and titles[2] in after


@pytest.mark.parametrize('word', ['올림픽', 'AI', '2026', 'R&D', 'A"B'])
def test_monthly_keyword_guide_skips_fragments_and_omits_empty_example(word):
    monthly = [dict(month='2026-01', count=1, top_keywords=[
                   dict(keyword='U-', mention_sum=162, lift=6.5),
                   dict(keyword='대표…', mention_sum=25, lift=4)]),
               dict(month='2026-02', count=1, top_keywords=[
                   dict(keyword='팀', mention_sum=20, lift=3),
                   dict(keyword=word, mention_sum=1592, lift=7.5)])]
    html = app.render_stats(dict(basis='전체', monthly=monthly))
    header = html.split('<thead>', 1)[1].split('</thead>', 1)[0]
    label = escape(word)
    assert ('주요 키워드</span><span class="keyword-guide" title="그 달에 평소보다 유난히 많이 나온 단어 최대 4개'
            f' · 예: {label}(1,592, ×7.5) = 그 달 1,592번 언급, 평소의 7.5배'
            ' · 검색어와 흔한 단어는 뺌">평소보다 많이 나온 단어 (언급 수, 평소 대비 배수)</span>') in header
    table = html.split('<tbody>', 1)[1]
    assert f'{label}(1,592, ×7.5)' in table and '팀(20, ×3.0)' in table
    assert 'U-' not in html and '대표…' not in html
    assert '그 달에 평소(검색 기간 전체)' not in html
    empty = app.render_stats(dict(basis='전체', monthly=monthly[:1]))
    assert '예:' not in empty and '<td>—</td>' in empty


def test_monthly_keyword_frequency_fallback_and_lift_display():
    sql, values = engine.stats_query(['코스피'], params={'keywords': ['코스피']})
    assert '코스피' in values[-1]
    assert '(SELECT count(*) FROM monthly_counts) > 1' in sql
    assert 'ORDER BY mention_sum DESC, keyword COLLATE "C"' in sql
    assert 'NOT EXISTS (SELECT 1 FROM top_keywords t WHERE t.month=keyword_sums.month)' in sql
    assert 'FROM frequency_ranked WHERE rank <= 4' in sql
    fallback = [dict(keyword=word, mention_sum=count, lift=None) for word, count in
                [('증시', 12), ('상승', 8), ('투자', 5), ('시장', 2)]]
    html = app.render_stats(dict(basis='검색', monthly=[
        dict(month='2026-10', count=3, top_keywords=[dict(keyword='거래', mention_sum=4, lift=2.0)]),
        dict(month='2026-11', count=14, top_keywords=fallback),
    ]))
    assert '거래(4, ×2.0)' in html and '증시(12)' in html
    assert '상승(8)' in html and '투자(5)' in html and '시장(2)' in html
    assert '기준 비교 불가 또는 조건 미충족 · 많이 나온 순' in html
    assert '예: 거래(4, ×2.0)' in html


def test_timeline_keeps_each_event_inside_its_month_including_months_without_summary():
    titles = ['홍명보호 32강 실패', '대표팀 귀국', '홍명보 자진사퇴', '축구협회 압수수색']
    dates = ['2026-06-24', '2026-06-30', '2026-08-18', '2026-10-22']
    items = [dict(date_label=day, group_id=index, article_count=index,
                  representative=dict(title=title, published_date=day, publisher='동아일보'))
             for index, (day, title) in enumerate(zip(dates, titles), 1)]
    items[1].update(date_from=dates[1], date_to='2026-07-02')
    months = [dict(month='2026-06', text='대표팀의 월드컵 도전이 마무리됐다', source='AI')]
    timeline = dict(items=items, months=months, model='qwen3:4b', llm_ms=1,
                    summary_status='통과', lead='국가대표팀 주요 사건을 정리했다.')
    panel = app.render_timeline(timeline, ['국가대표'])
    flow, after_flow = panel.split('</ul><p class="hint flow-notice">', 1)
    _, june, august, october = flow.split('<details class="flow-month-details">')
    assert '>6월</strong>' in june and '>흐름 요약</small>' in june and '(사건 2 · 보도 3건)' in june
    assert '>8월</strong>' in august and '>대표 제목</small>' in august and '>10월</strong>' in october
    for month, expected in ((june, titles[:2]), (august, titles[2:3]), (october, titles[3:])):
        summary, events = month.split('</summary>', 1)
        assert 'timeline-event' not in summary
        assert events.count('class="timeline-event"') == len(expected)
        assert events.count('동일 보도 보기') == events.count('후속 기사 보기') == len(expected)
        assert all((title in events) == (title in expected) for title in titles)
    assert 'timeline-event' not in after_flow and '펼쳐서 더보기' not in panel
    assert 'AI 요약 보기' in after_flow and '요약 검사: 통과' in after_flow

    single_month = app.render_timeline(dict(timeline, items=items[:2]), ['국가대표'])
    assert single_month.count('<details class="flow-month-details">') == 1
    assert single_month.count('class="timeline-event"') == 2
