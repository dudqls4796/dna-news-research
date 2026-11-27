from collections import OrderedDict
import re

import pytest

from search import app, engine


def event(group_id, title, article_count=1, score=.9, **ranks):
    day = f'2026-01-{group_id:02}'
    return {'group_id': group_id, 'date_from': day, 'date_to': day, 'article_count': article_count,
            'score': score, 'representative': {'title': title}, 'publishers': [], 'matches': [], **ranks}


def test_relevance_selection_prioritizes_level_then_coverage_then_similarity():
    events = [event(1, '간접 관련 대형 사건', 100, .99, relevance=1),
              event(2, '직접 관련 소형 사건', 1, .99, relevance=2),
              event(3, '직접 관련 다수 보도', 7, .88, relevance=2),
              event(4, '직접 관련 다수 보도와 높은 유사도', 7, .91, relevance=2)]
    result = {'params': {'q': '사건 원인'}, 'events': events}
    assert [e['group_id'] for e in engine.select_timeline_events(result, events, limit=1)] == [4]
    assert [e['group_id'] for e in engine.select_timeline_events(result, events, limit=2)] == [3, 4]
    assert [e['group_id'] for e in engine.select_timeline_events(result, events, limit=3)] == [2, 3, 4]


def test_event_cap_keeps_each_month_then_fills_by_coverage():
    may = [dict(event(i, '러브버그 5월 방제', 30 - i),
                date_from=f'2026-05-{i:02}', date_to=f'2026-05-{i:02}') for i in range(1, 17)]
    june = dict(event(17, '러브버그 6월 확산', 2), date_from='2026-06-01', date_to='2026-06-01')
    july = dict(event(18, '러브버그 7월 민원 감소'), date_from='2026-07-07', date_to='2026-07-13')
    events = [*may, june, july]
    result = {'events': events}
    assert engine.select_timeline_events(result, events, limit=15) == [*may[:13], june, july]
    assert engine.select_timeline_events(result, events) == [*may[:8], june, july]


def test_event_cap_spreads_over_more_months_than_rows():
    events = [dict(event(i, '월별 소식', 20 - i),
                   date_from=f'2026-{i:02}-01', date_to=f'2026-{i:02}-01') for i in range(1, 13)]
    selected = engine.select_timeline_events({'events': events}, events)
    ids = [item['group_id'] for item in selected]
    assert len(ids) == 10 and ids[0] == 1 and ids[-1] == 12
    assert max(right - left for left, right in zip(ids, ids[1:])) == 2


def test_timeline_importance_selects_multi_publisher_event_and_keeps_months():
    volume = dict(event(1, '한 언론사의 반복 보도', 30, .99), publishers=['동아일보', '동아일보'])
    broad = dict(event(2, '여러 언론사가 다룬 사건', 3, .85), publishers=['동아일보', '한국일보'])
    brief = dict(event(3, '한 언론사의 짧은 보도', 1, .98), publishers=['한겨레'])
    june = dict(event(4, '6월 사건'), date_from='2026-06-01', date_to='2026-06-01')
    events = [volume, broad, brief, june]
    result = {'events': events}
    assert engine.select_timeline_events(result, events, limit=1) == [broad]
    assert engine.select_timeline_events(result, events, limit=2) == [broad, june]
    month = engine.timeline_month_input(events)[0]
    assert engine.timeline_month_representative(month['events'])['title'] == broad['representative']['title']
    assert broad['representative']['title'] in {item['title'] for item in engine.month_coverage_titles(month)}


def test_timeline_merge_recomputes_coverage_and_uses_best_relevance_score():
    left = dict(event(1, '첫 보도', 2, .8), publishers=['동아일보', '한겨레'])
    right = dict(event(2, '이튿날 보도', 3, .9), publishers=['동아일보', '한국일보'])
    centers = {(True, 1): [1.0], (True, 2): [1.0]}
    keywords = {key: {'복귀'} for key in centers}
    merged, = engine.merge_timeline_events([left, right], centers, keywords)
    assert merged['publisher_count'] == 3 and merged['article_count'] == 5 and merged['score'] == .9
    assert merged['importance'] == pytest.approx(4 - 1 / (6 + 1.9 / 4))
    assert [sub['publisher_count'] for sub in merged['sub_events']] == [2, 2]
    assert engine.event_coverage(event(3, '언론사 미상', 1, None))['publisher_count'] == 0


def test_result_importance_breaks_relevance_ties_only():
    volume = dict(event(1, '한 언론사 집중 보도', 20, .9), latest_match='2026-01-30', publishers=['동아일보'])
    broad = dict(event(2, '여러 언론사의 보도', 3, .9), latest_match='2026-01-20', publishers=['동아일보', '한겨레'])
    relevant = dict(event(3, '질문과 더 가까운 보도', 1, .91), latest_match='2026-01-10', publishers=['동아일보'])
    events = [volume, broad, relevant]
    assert engine.sort_result_events(events, 'combined', similarity=True) == [relevant, broad, volume]
    assert engine.sort_result_events(events, 'combined', importance_tiebreak=False) == events
    same_day = [dict(item, score=None, latest_match='2026-01-30') for item in events]
    assert engine.sort_result_events(same_day, 'keyword') == [same_day[1], same_day[0], same_day[2]]
    assert engine.sort_result_events(same_day, 'keyword', importance_tiebreak=False) == same_day


def test_relevance_keeps_market_context_and_excludes_unconnected_good_news(monkeypatch):
    events = [event(1, '주주환원 기대 미달로 주가 급락'),
              event(2, '시장 급락에 매도 사이드카 발동'),
              event(3, '분기 영업이익 사상 최대')]

    def chat(system, data, schema, _deadline):
        assert data.splitlines()[3:] == [f'{i}. {e["representative"]["title"]}' for i, e in enumerate(events, 1)]
        assert schema['required'] == ['1', '2', '3']
        assert all(p['enum'] == [0, 1, 2] for p in schema['properties'].values())
        return {'1': 2, '2': 1, '3': 0, '999': 2}, 7

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, elapsed = engine.judge_timeline_relevance(events, {'keywords': ['기업'], 'q': '주가 하락 원인'})
    assert [(e['group_id'], e['relevance']) for e in selected] == [(1, 2), (2, 1)]
    assert elapsed == 7 and all('relevance' not in e for e in events)


@pytest.mark.parametrize('decision', [None, True, 3, '1'])
def test_invalid_relevance_decisions_fail_as_a_batch(monkeypatch, decision):
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: ({'1': decision}, 1))
    with pytest.raises(engine.LLMError):
        engine.judge_timeline_relevance([event(1, '기사 제목')], {'keywords': [], 'q': '사건 원인'})


def test_coverage_candidate_requires_successful_relevance_judgement(monkeypatch):
    results = [event(1, '직접 관련 사건', similarity_rank=1),
               event(2, '보도가 많은 시장 요인', 30, .7,
                     similarity_rank=engine.KEYWORD_TIMELINE_CANDIDATES + 1, coverage_rank=1)]
    result = {'params': {'q': '사건 원인', 'keywords': ['대상'], 'limit': 20}, 'events': results[:1]}
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', lambda _: results)
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda _: {(True, 1): [1]})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1]')
    monkeypatch.setattr(engine, 'timeline_centers', lambda _: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    monkeypatch.setattr(engine, 'TIMELINE_RELEVANCE_BATCH', 1)

    def offline(_system, _data, _schema, _deadline, **_options):
        raise engine.LLMError('판정 시간 초과')

    monkeypatch.setattr(engine, 'ollama_chat', offline)
    fallback = engine.generate_timeline(result)
    assert fallback['relevance_status'] == '판정 실패' and fallback['llm_calls'] == 2
    assert fallback['items'] == [] and not engine._timeline_cache
    assert '관련성 판정을 완료하지 못했습니다' in app.render_timeline(fallback, ['대상'])

    calls = []

    def retry(system, data, schema, deadline, **_options):
        numbers = schema['required']
        calls.append((numbers, deadline))
        if numbers == ['r']:
            assert data.splitlines()[3:] == ['[유사도 후보]', 'A. 직접 관련 사건',
                                             '[보도량 후보]', 'B. 보도가 많은 시장 요인']
            return {'r': 'A2'}, 1  # 항목이 모자란 압축 결과는 번호별 배치로 대체한다.
        assert numbers == ['1', '2'] and engine.prompts.TIMELINE_COVERAGE in system
        assert data.splitlines()[3:] == ['[유사도 후보]', '1. 직접 관련 사건',
                                         '[보도량 후보]', '2. 보도가 많은 시장 요인']
        return {'1': 2, '2': 1}, 3

    monkeypatch.setattr(engine, 'ollama_chat', retry)
    selected = engine.generate_timeline(result)
    assert selected['relevance_status'] == '판정 완료' and selected['llm_calls'] == 2
    assert selected['summary_attempts'] == 0 and selected['relevance_ms'] >= 3
    assert [numbers for numbers, _ in calls] == [['r'], ['1', '2']]
    assert calls[0][1] < calls[1][1]
    assert [(e['group_id'], e['relevance']) for e in selected['items']] == [(1, 2), (2, 1)]

    engine._timeline_cache.clear()
    answers = iter([({'r': 'A2,B1'}, 3), ({'lead': ''}, 1)])
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_, **__: next(answers))
    batched = engine.generate_timeline(result)
    assert batched['relevance_status'] == '판정 완료' and batched['llm_calls'] == 2
    assert batched['items'] == selected['items'] and batched['relevance_excluded_count'] == 0


@pytest.mark.parametrize('grades', ['A0,B0,C0,D0,E0,F0,G0,H0,I0', 'A2,B1,C2,D1,E2,F1,G2,H1,I2',
                                    'A2,B0,C1,D0,E0,F0,G0,H0,I3', 'A2,A1,C0,D0,E0,F0,G0,H0,I0'])
def test_invalid_or_degenerate_compact_relevance_requires_fallback(monkeypatch, grades):
    events = [event(i, '기사 제목') for i in range(1, 10)]
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_, **__: ({'r': grades}, 1))
    with pytest.raises(engine.LLMError):
        engine.judge_timeline_relevance(events, {'keywords': [], 'q': '주제'},
                                         batch_groups=[(4, False), (5, False)], compact=True)


def test_named_topic_batch_keeps_coverage_boundary_and_grades(monkeypatch):
    events = [event(1, '코스피 장중 급락'), event(2, '매수 사이드카 발동'),
              event(3, '시중은행 금리 동향')]

    def chat(system, data, schema, _deadline):
        assert engine.prompts.NAMED_PRICE_RELEVANCE in system and '질문: 코스피' in data
        assert data.splitlines()[3:] == ['[유사도 후보]', '1. 코스피 장중 급락',
                                         '[보도량 후보]', '2. 매수 사이드카 발동', '3. 시중은행 금리 동향']
        assert schema['required'] == ['1', '2', '3']
        return {'1': 2, '2': 1, '3': 0}, 4

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, elapsed = engine.judge_timeline_relevance(
        events, {'keywords': ['코스피'], 'q': '코스피'}, batch_groups=[(1, False), (2, True)])
    assert [(e['group_id'], e['relevance']) for e in selected] == [(1, 2), (2, 1)] and elapsed == 4


@pytest.mark.parametrize('compact', [False, True])
def test_bare_topic_asks_for_its_own_events_in_both_judging_formats(monkeypatch, compact):
    events = [event(1, '러브버그 출몰 앞두고 계양산 친환경 포집기 투입'),
              event(2, '러브버그 잠잠해지자 갈색여치 확산'),
              event(3, '인천 러브버그 민원 88% 감소')]

    def chat(system, data, _schema, _deadline, **_options):
        assert engine.prompts.NAMED_TOPIC_RELEVANCE in system
        assert engine.prompts.PRICE_RELEVANCE not in system
        assert data.splitlines()[-1] == ('C. 인천 러브버그 민원 88% 감소 [보도량 후보]' if compact
                                         else '3. 인천 러브버그 민원 88% 감소')
        return ({'r': 'A2,B0,C2'} if compact else {'1': 2, '2': 0, '3': 2}), 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(
        events, {'q': '러브버그', 'keywords': ['러브버그']},
        batch_groups=[(2, False), (1, True)] if compact else None, compact=compact)
    assert [item['group_id'] for item in selected] == [1, 3]


@pytest.mark.parametrize('compact', [False, True])
def test_stock_period_relevance_keeps_observed_moves_and_excludes_outlook_only(monkeypatch, compact):
    events = [event(1, '삼성전자 영업익 100조 예상 주가 반등할까'),
              event(2, '삼성전자 주가 4% 하락 반등 전망은'),
              event(3, '코스피 급락에 매도 사이드카 발동')]

    def chat(system, _data, _schema, _deadline, **_options):
        assert engine.prompts.PRICE_RELEVANCE in system
        return ({'r': 'A0,B2,C1'} if compact else {'1': 0, '2': 2, '3': 1}), 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(
        events, {'q': '삼성전자 주가', 'keywords': ['삼성전자']},
        batch_groups=[(2, False), (1, True)] if compact else None, compact=compact)
    assert [(item['group_id'], item['relevance']) for item in selected] == [(2, 2), (3, 1)]


def test_earnings_relevance_requires_an_earnings_link_for_market_news(monkeypatch):
    events = [event(1, 'SK하이닉스 삼성전자 제치고 시총 1위'),
              event(2, 'SK하이닉스 1분기 사상 최대 영업익에 주가 급등'),
              event(3, '코스피 상승에 SK하이닉스 강세', summary='SK하이닉스가 예상을 웃도는 매출을 발표했다.')]
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_, **__: ({'r': 'A0,B2,C1'}, 1))
    selected, _ = engine.judge_timeline_relevance(
        events, {'q': 'SK하이닉스 실적', 'keywords': ['SK하이닉스']},
        batch_groups=[(1, False), (2, True)], compact=True)
    assert [(item['group_id'], item['relevance']) for item in selected] == [(2, 2), (3, 1)]


@pytest.mark.parametrize('compact', [False, True])
def test_earnings_prompt_distinguishes_compensation_and_personal_returns(monkeypatch, compact):
    events = [
        event(1, '대체 얼마야? 삼성 SK하이닉스, 역대급 성과급 받는다', summary='작년 영업익이 늘었다.'),
        event(2, '연봉 1억에 1.5억 더? SK하이닉스 2964% 성과급 지급'),
        event(3, 'SK하닉 평균 연봉 1억8500만원 1년새 6800만원 올랐다'),
        event(4, 'SK하이닉스 수익률 2500% 인증 글 화제'),
        event(5, 'SK하이닉스, 3분기 채용 일정 발표'),
        event(6, '영업이익 83조-매출 104조 SK하이닉스 분기 최대 실적', 43),
        event(7, 'SK하이닉스 2분기 어닝 서프라이즈'),
        event(8, 'SK하이닉스 1분기 흑자 전환'),
        event(9, 'SK하이닉스 개미 수익률 2500%', summary='SK하이닉스 분기 영업익은 83조원이었다.'),
    ]

    def chat(system, data, schema, _deadline, **_options):
        assert engine.prompts.TIMELINE_ASPECT_RELEVANCE in system and '남은 주제어: 실적' in data
        assert engine.prompts.TIMELINE_EARNINGS_RELEVANCE in system
        grades = [0, 0, 0, 0, 0, 2, 2, 2, 0]
        return ({'r': 'A0,B0,C0,D0,E0,F2,G2,H2,I0'} if compact else dict(zip(schema['required'], grades))), 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(
        events, {'keywords': ['SK하이닉스'], 'q': 'SK하이닉스 실적'},
        batch_groups=[(5, False), (4, True)], compact=compact)
    assert [item['group_id'] for item in selected] == [6, 7, 8]


@pytest.mark.parametrize('query, keywords, terms, related, unrelated', [
    ('현대차 미국 관세', ['현대차', '미국'], ['관세'], '미국, 현대차 수입차에 15% 수입세 적용', '현대차 미국 공장 증설'),
    ('현대차 미국 관세', ['현대차'], ['미국', '관세'], '미국, 현대차 수입차에 15% 수입세 적용', '현대차 미국 공장 증설'),
    ('러브버그 방역', ['러브버그'], ['방역'], '계양산 러브버그 포집기 투입', '러브버그 떼가 민가에 출몰'),
    ('손흥민 이적', ['손흥민'], ['이적'], '손흥민 LAFC와 3년 계약 서명', '손흥민 리그 경기 멀티골'),
    ('국민의힘 전당대회', ['국민의힘'], ['전당대회'], '국민의힘 당대표 선출 경선 일정 확정', '국민의힘 원내대표 사퇴'),
    ('윤석열 재판 결과', ['윤석열'], ['재판', '결과'], '윤석열 징역 5년 확정', '윤석열 자택 인근 집회'),
    ('인공지능 규제', ['인공지능'], ['규제'], 'AI 모델을 공개하기 전 정부 심사 의무화', '인공지능 투자 확대'),
    ('가람항 저소음 선박 야간 하역', ['가람항'], ['저소음', '선박', '야간', '하역'],
     '가람항, 밤에 소음 억제형 배에서 짐 내려', '가람항 일반 선박 주간 하역 시작'),
])
@pytest.mark.parametrize('compact', [False, True])
def test_batch_judges_any_aspect_and_accepts_semantic_matches(monkeypatch, query, keywords, terms,
                                                         related, unrelated, compact):
    params = {'q': query, 'keywords': keywords}
    assert engine.timeline_aspect_terms(params) == terms

    def chat(system, data, _schema, _deadline, **_options):
        assert engine.prompts.TIMELINE_ASPECT_RELEVANCE in system
        assert '남은 주제어: ' + ' '.join(terms) in data
        assert related in data and unrelated in data
        return ({'r': 'A2,B0'} if compact else {'1': 2, '2': 0}), 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(
        [event(1, related), event(2, unrelated)], params, batch_groups=[(1, False), (1, True)], compact=compact)
    assert [item['group_id'] for item in selected] == [1]


@pytest.mark.parametrize('topic, keyword', [
    ('러브버그의 최신 뉴스·소식·관련 기사·근황은', '러브버그'),
    ('2026년 10월 삼성 전자 관련 뉴스', '삼성전자'),
    ('삼전 근황', '삼성전자'),
])
def test_subject_alias_particles_and_generic_news_words_leave_no_aspect(monkeypatch, topic, keyword):
    params = {'q': topic, 'keywords': [keyword]}
    assert engine.timeline_aspect_terms(params) == []

    def chat(system, data, _schema, _deadline):
        assert engine.prompts.NAMED_TOPIC_RELEVANCE in system
        assert '남은 주제어' not in data
        return {'1': 2}, 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance([event(1, f'{keyword} 관련 새 사건')], params)
    assert [item['group_id'] for item in selected] == [1]


def test_topic_only_batch_also_checks_each_event_against_the_aspect(monkeypatch):
    params = {'q': '손흥민 이적', 'keywords': []}

    def chat(system, data, _schema, _deadline, **_options):
        assert engine.prompts.TIMELINE_ASPECT_RELEVANCE in system
        assert data['question'] == params['q'] and data['aspect_terms'] == ['손흥민', '이적']
        return {'r': 'A2,B0'}, 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(
        [event(1, '손흥민 LAFC와 3년 계약'), event(2, '손흥민 팀 동료가 다른 팀에 합류')],
        params, compact=True)
    assert [item['group_id'] for item in selected] == [1]


@pytest.mark.parametrize('query, keyword, aspect, direct_titles, other_titles', [
    ('손흥민 이적', '손흥민', '이적',
     ['LAFC, 손흥민 영입 발표', '손흥민과 동료가 함께 새 팀으로 이적'],
     ['이적설 부앙가, LAFC와 재계약', '토트넘, 손흥민 이적 후 11경기째 무승',
      '메시 손흥민 이어 그리즈만도 MLS 이적', '레반도프스키, 시카고로 이적... 손흥민 메시와 뛴다',
      '손흥민 후계자인 줄 알았는데 토트넘 존슨, 팰리스로 이적', '美이적후 13경기 12골 초절정 손',
      '손흥민 떠난 EPL에 다시 한류 훈풍 불까...김민재 오현규 이적설', '지프, LA FC 이적 손흥민 ‘백업’ 예상',
      '손흥민 3연속골 이강인 665억 이적', '오현규, 튀르키예 베식타시 입단 이적료 약 241억 원']),
    ('국민의힘 전당대회', '국민의힘', '전당대회',
     ['국힘 차기 당권 경쟁', '유승민 “내년 전당대회 중요 역할 할 것”'],
     ['정청래 “6·3 지방선거에 모든 것 건다 당대표 권한인 전략공천 안해”',
      '국민의힘, 당명 변경안 확정', '국민의힘, 김민석 민주당 새 대표 선출 축하']),
])
@pytest.mark.parametrize('compact', [False, True])
def test_batch_excludes_background_and_other_parties_even_when_model_accepts(
        monkeypatch, query, keyword, aspect, direct_titles, other_titles, compact):
    params = {'q': query, 'keywords': [keyword], 'target_category': '스포츠' if aspect == '이적' else '정치'}
    assert engine.timeline_aspect_terms(params) == [aspect]
    events = [event(i, title, summary='제목을 뒷받침하지 않는 별개 사건')
              for i, title in enumerate([*direct_titles, *other_titles], 1)]

    def chat(system, data, schema, _deadline, **_options):
        assert f'남은 주제어: {aspect}' in data
        assert all(f'[제외] {title}' in data for title in other_titles)
        assert data.count('제목을 뒷받침하지 않는 별개 사건') == len(direct_titles)
        if compact:
            return {'r': ','.join(f'{chr(65 + i)}2' for i in range(len(events)))}, 1
        return dict.fromkeys(schema['required'], 2), 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    selected, _ = engine.judge_timeline_relevance(events, params, compact=compact)
    assert [item['representative']['title'] for item in selected] == direct_titles


def test_inferred_person_title_remains_context():
    assert engine.timeline_aspect_terms(
        {'q': '변호사 김도영', 'keywords': ['김도영'], 'target_category': '사회'}) == []


def test_unregistered_aspect_prioritizes_candidate_titles_without_discarding_other_candidates():
    params = {'q': '가람항 야간 하역', 'keywords': ['가람항']}
    signals, aspect = engine.timeline_title_signals(params)
    assert signals and re.search(aspect, '가람항 하역 야간 허용') and not re.search(aspect, '가람항 하역 금지')
    sql, values = engine.keyword_timeline_query(params, '[1]')
    assert 'aspect_match DESC' in sql and 'similarity_rank <= %s OR coverage_rank <= %s' in sql
    assert sql.count('%s') == len(values)


@pytest.mark.parametrize('has_market_event', [True, False])
def test_stock_aspect_controls_month_summary_and_expanded_events(monkeypatch, has_market_event):
    earnings = event(1, '삼성전자 3분기 영업익 최고 기록', 100, .99)
    price = event(2, '삼성전자, 52주 신고가 경신', 6, .79, aspect_match=True)
    market = event(3, '코스피 장중 급락', 80, .90, aspect_match=True)
    middle = event(18, '삼전닉스 금융위기급 폭락에 코스피 반등 재료는', aspect_match=True)
    late = event(29, '삼성, 반도체만 3분기 영업익 106조인데 주가는 되레 4% 하락', aspect_match=True)
    expected = [price, market, middle, late] if has_market_event else []
    events = [earnings, *expected]
    for item in events:
        item['date_from'] = item['date_to'] = item['date_from'].replace('-01-', '-10-')
    params = {'q': '삼성전자 주가', 'keywords': ['삼성전자'], 'limit': 20}
    result = {'params': params, 'events': events}
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', lambda _: events)
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda _: {})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1]')
    monkeypatch.setattr(engine, 'timeline_centers', lambda _: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    def judge(_s, data, schema, _t):
        # 위치가 아니라 제목으로 판정해, 관점 사전 필터로 후보 순서가 바뀌어도 같은 결과를 낸다.
        titles = dict(re.findall(r'^(\d+)\. (.+)$', str(data), re.M))
        return {n: 0 if titles.get(n) == earnings['representative']['title'] else 2 for n in schema['required']}, 1

    monkeypatch.setattr(engine, 'ollama_chat', judge)

    def summarize(months, _, _deadline, *, aspect_subject):
        assert [item['title'] for item in months[0]['events']] == [item['representative']['title'] for item in expected]
        return {'months': []}, 1

    monkeypatch.setattr(engine, 'summarize_timeline', summarize)
    timeline = engine.generate_timeline(result)
    assert [item['group_id'] for item in timeline['items']] == [item['group_id'] for item in expected]
    panel = app.render_timeline(timeline, params['keywords'])
    assert panel.count('class="timeline-event"') == len(expected)
    if has_market_event:
        assert timeline['months'][0]['text'] == '10/2 52주 신고가 → 10/18 폭락 → 10/29 영업익 106조인데 주가 4% 하락'
        assert '>제목 기반 흐름</small>' in panel and earnings['representative']['title'] not in panel
    else:
        assert timeline['months'] == [] and '질문과 관련된 사건을 찾지 못했습니다' in panel


@pytest.mark.parametrize('topic, title, end, period', [
    ('SK하이닉스 실적', 'SK하이닉스 3분기 영업이익 83조', '2026-11-20', '2026.01 ~ 2026.11'),
    ('SK하이닉스 실적', 'SK하이닉스 3분기 영업이익 83조', None, '2026.10 ~ 2026.10'),
    ('쿠팡 개인정보 유출', '쿠팡 개인정보 유출 조사 결과 발표', '2026-11-20', '2026.01 ~ 2026.11'),
])
def test_header_uses_full_aspect_and_consistent_period(monkeypatch, topic, title, end, period):
    params = {'keywords': [topic.split()[0]], 'q': topic, 'date_from': '2026-01-01', 'date_to': end}
    incident = dict(event(1, title, grouped=True), date_from='2026-10-19', date_to='2026-10-30')
    result = {'params': params, 'events': [incident]}
    timeline = {**engine.match_timeline(result), 'llm_ms': 0}
    monkeypatch.setattr(app, 'handle_query', lambda *_a, **_kw: (result, 200))
    monkeypatch.setattr(app, 'generate_timeline', lambda _: timeline)
    monkeypatch.setattr(app, 'timeline_articles', lambda *_: ({}, {}))

    html, status = app.handle_timeline('fragment=1')

    assert status == 200 and f'<h2>{topic} · {period}</h2>' in html
    assert '<strong class="flow-month">10월</strong>' in html


def test_stock_aspect_uses_title_synonyms_and_prioritizes_matching_result():
    params = engine.validate_params({'keywords': '삼전', 'q': '삼전 주가'})
    name, context, _, _ = engine.title_signals(params)
    assert re.search(name, '삼성전자') and re.search(name, '삼전')
    for title in ('주가', '주식', '증시', '코스피', '시총', '52 주', '신고가', '급등', '급락', '외국인 순매수'):
        assert re.search(context, title)
    assert not re.search(context, '삼성전자 3분기 영업익 107.4조')
    for sql, values in (engine.candidate_query(params, '[]', [1]), engine.keyword_timeline_query(params, '[]')):
        assert 'context_match DESC' in sql and sql.count('%s') == len(values)

    rows = [dict(id=1, group_id=10, published_date='2026-10-31', score=.99, aspect_match=False),
            dict(id=2, group_id=10, published_date='2026-10-29', score=.78, aspect_match=True),
            dict(id=3, group_id=11, published_date='2026-10-30', score=.99, aspect_match=False)]
    events = engine.sort_result_events([dict(item, article_count=1) for item in engine.fold_events(rows, 'combined')],
                                       'combined', similarity=True)
    assert [item['matches'][0]['id'] for item in events] == [2, 3]


def test_non_price_month_flow_keeps_dates_and_whole_titles_in_one_summary_call(monkeypatch):
    dates = ['2026-06-03', '2026-06-14', '2026-06-29', '2026-07-02', '2026-07-16', '2026-07-25']
    titles = ['러브버그 첫 출몰 신고', '도심 러브버그 확산', '방제 작업 시작',
              '러브버그 집중 방제', '러브버그 방제 지역 확대', '러브버그 신고 감소']
    events = [dict(event(i, title), date_from=day, date_to=day)
              for i, (day, title) in enumerate(zip(dates, titles), 1)]
    result = {'params': {'q': '', 'keywords': ['러브버그'], 'limit': 20}, 'events': events}
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', lambda _: events)
    monkeypatch.setattr(engine, 'timeline_centers', lambda _: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    calls = []

    monkeypatch.setenv('LLM_THINK', '0')

    def chat(_system, data, schema, deadline, *, num_predict):
        calls.append(deadline)
        assert schema['required'] == ['context', 'lead'] and 0 < num_predict < 400
        assert data['events'] == data['displayed_flow'] == titles
        return {'lead': '', 'context': ''}, 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    timeline = engine.generate_timeline(result)
    assert len(calls) == timeline['llm_calls'] == 1 and calls[0] is not None
    assert [month['text'] for month in timeline['months']] == [
        '6/3 러브버그 첫 출몰 신고 → 6/14 도심 러브버그 확산 → 6/29 방제 작업 시작',
        '7/2 러브버그 집중 방제 → 7/16 러브버그 방제 지역 확대 → 7/25 러브버그 신고 감소']
    panel = app.render_timeline(timeline, ['러브버그'])
    assert panel.count('>제목 기반 흐름</small>') == 2 and panel.count('class="timeline-event"') == 6
    assert 'AI 흐름 검사' not in panel

    def unavailable(*_, **__):
        raise engine.LLMError('응답 시간 초과')

    engine._timeline_cache.clear()
    monkeypatch.setattr(engine, 'ollama_chat', unavailable)
    fallback = engine.generate_timeline(result)
    assert fallback['llm_calls'] == 1
    assert fallback['months'] == timeline['months']


@pytest.mark.parametrize('query, price', [
    ('SK하이닉스 실적', False), ('쿠팡 개인정보 유출', False), ('삼성전자 주가 조작', False),
    ('삼성전자 주가', True), ('코스피', True), ('원달러 환율', True), ('기준금리 인하', True),
])
def test_direction_rules_apply_only_to_price_queries(query, price):
    events = [event(3, '코스피 8.3% 폭락', 1), event(10, '쿠팡 정보 유출 3000만명 확인', 12),
              event(16, '코스피 장중 회복', 2), event(19, '피해자 첫 재판', 10),
              event(26, '쿠팡 대표 경찰 출석', 7)]
    monthly = engine.timeline_month_input(events)
    assert engine.is_price_timeline({'q': query, 'keywords': []}) is price
    rows = engine.match_timeline_months({}, monthly, price=price)['months']
    if price:
        assert rows[0]['text'] == '1/3 8.3% 폭락 → 1/16 회복'
    else:
        assert rows[0]['text'] == '1/10 쿠팡 정보 유출 3000만명 확인 → 1/19 피해자 첫 재판 → 1/26 쿠팡 대표 경찰 출석'


def test_samsung_timeline_keeps_the_month_flow_and_renders_the_grounded_lead(monkeypatch):
    dates = ('2026-10-06', '2026-10-13', '2026-10-29')
    titles = ('삼성전자 1.63% 하락', '코스피 5%대 급락·사이드카', '삼성 영업익 106조인데 주가 4% 하락')
    headlines = ('삼성전자 1.63% 하락 속 HLB글로벌 19.99% 급등',
                 '[속보]코스피 5%대 급락, 매도 사이드카 55일만에 발동',
                 '삼성, 반도체만 3분기 영업익 106조인데 주가는 되레 4% 하락')
    events = [dict(event(i, title, aspect_match=True), date_from=day, date_to=day)
              for i, (day, title) in enumerate(zip(dates, headlines), 1)]
    events.append(dict(event(4, '삼성전자 분기 영업이익 100조 시대 열까 주가 반등 전망', 100, aspect_match=True),
                       date_from='2026-10-08', date_to='2026-10-08'))
    params = {'q': '삼성전자 주가', 'keywords': ['삼성전자'], 'limit': 20}
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', lambda _: events)
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda _: {})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1]')
    monkeypatch.setattr(engine, 'timeline_centers', lambda _: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    monkeypatch.setattr(engine, 'judge_timeline_relevance', lambda pool, *_, **__: (pool, 1))
    events[-1]['summary'] = '삼성전자 하락 배경으로 외국인 순매도 확대가 꼽혔다.'
    good = '삼성전자 하락 배경으로 외국인 순매도 확대가 꼽혔다. 반도체 영업이익은 106조였다.'
    context = '외국인 순매도 확대'
    lead = '10월 1일 ' + good

    def chat(_system, data, schema, _deadline, **_options):
        assert data['displayed_flow'] == list(titles)
        assert data['events'][1].endswith(events[-1]['summary'])
        assert schema['required'] == ['context', 'lead']
        return {'lead': lead, 'context': context}, 1

    monkeypatch.setattr(engine, 'ollama_chat', chat)
    result = {'params': params, 'events': events}
    rejected = engine.generate_timeline(result)
    assert rejected['summary_status'] == '생략' and rejected['lead'] is None
    assert '항목 날짜에 없는 월일' in rejected['summary_checks'][0]['reasons']
    rejected_panel = app.render_timeline(rejected, params['keywords'])
    assert 'AI 요약 보기' not in rejected_panel and '10월 1일' not in rejected_panel

    engine._timeline_cache.clear()
    lead = '삼성전자 주가는 영업익 106조에도 1.63% 하락했다. 배경으로 외국인 순매도 확대가 꼽혔다.'
    mixed = engine.generate_timeline(result)
    assert mixed['lead'] is None
    assert mixed['summary_checks'][0]['reasons'] == ['다른 사건의 수치 결합']

    engine._timeline_cache.clear()
    lead = '삼성전자 주가가 1.63% 하락했다. 코스피는 5%대 급락했다. 삼성 영업익 106조에도 주가는 4% 하락했다.'
    context = '1.63% 하락'
    duplicate = engine.generate_timeline(result)
    assert duplicate['lead'] is None
    assert duplicate['summary_checks'][0]['reasons'] == ['월 흐름에 없는 기사 근거 누락']

    engine._timeline_cache.clear()
    lead = good
    context = '외국인 순매도 확대'
    valid = engine.generate_timeline(result)
    assert valid['months'] == rejected['months']
    assert valid['months'][0]['text'] == f'10월 초 {titles[0]} → 중순 {titles[1]} → 말 {titles[2]}'
    panel = app.render_timeline(valid, params['keywords'])
    assert 'AI 요약 보기' in panel and '요약 검사: 통과' in panel and good in panel
