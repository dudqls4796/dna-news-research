"""본문 비저장/수집 범위/검증 및 타임라인 근거 회귀 테스트."""
from unittest.mock import patch, Mock

from db import summarize_groups as batch
from search import app, engine


def test_numeric_dates_and_sentences():
    body = '2026년 10월 18일 삼성전자가 수출 20% 증가를 발표했다. 이익은 늘었다.'
    assert batch.validate_body_summary('삼성전자가 수출 20% 증가를 발표했다. 이익은 늘었다.', body)
    for text in ('수출이 30% 증가했다고 밝혔다. 이익도 함께 늘어난 것으로 나타났다.',
                 '10월 19일 수출이 증가했다고 밝혔다. 이익도 함께 늘어난 것으로 나타났다.',
                 '수출이 증가했다고 밝혔다.'):
        assert not batch.validate_body_summary(text, body)


def test_domain_and_redirect_guard():
    for url in ('https://www.donga.com.evil.com/a', 'https://www.khan.co.kr/a',
                'https://user@www.donga.com/a', None):
        assert not batch.donga_url(url)
    assert batch.donga_url('https://www.donga.com/news/article/1')
    session = batch.DongaSession()
    with patch('requests.Session.get') as get:
        try:
            session.get('https://www.khan.co.kr/news/1')
        except ValueError:
            pass
        else:
            raise AssertionError('다른 언론사 요청 허용')
        assert not get.called
    state = batch.TrialState(gap=1)
    try:
        state['gap'] = 60
    except ValueError:
        assert state['gap'] == 60
    else:
        raise AssertionError('긴 Crawl-delay 허용')


def test_summary_failure_never_returns_body():
    state = {'session': Mock(), 'gap': 1}
    with patch.object(batch, 'fetch', side_effect=ValueError('민감한 원문')):
        assert batch.summarize('https://www.donga.com/news/1', state) == (None, '원문 읽기 또는 요약 실패')


def test_timeline_summary_evidence():
    event = dict(date_from='2026-10-18', date_to='2026-10-18', article_count=3,
                 representative={'title': '삼성전자 수출 실적 발표'}, summary='수출이 20% 늘었다. 이익이 증가했다.')
    item = engine.timeline_summary_input([event])[0]
    assert item['summary'] == event['summary']
    month = engine.timeline_month_input([event])[0]
    assert month['events'][0]['summary'] == event['summary']
    assert engine.validate_month_text('삼성전자 수출 20% 증가', month) == [
        '근거 제목에 없는 숫자·단위: 20%', '근거 제목에 없는 방향']
    assert 'summary' not in engine.timeline_summary_input([{k: v for k, v in event.items() if k != 'summary'}])[0]


def test_month_with_body_summary_keeps_length_and_grounding_checks():
    event = dict(date_from='2026-11-17', date_to='2026-11-17', date_label='2026-11-17', article_count=60,
                 representative={'title': '與 초선 55명, 최민희 거취 판단 촉구 성명'},
                 summary='초선 55명이 최민희 의원에게 “정치적 거취를 진지하게 판단하라”는 성명을 발표했다.')
    first = dict(event, date_from='2026-11-05', date_to='2026-11-05', date_label='2026-11-05', summary=None,
                 representative={'title': '방송 정책을 놓고 여야 공방'})
    last = dict(event, date_from='2026-11-24', date_to='2026-11-24', date_label='2026-11-24', summary=None,
                representative={'title': '의원 거취 논의 재개'})
    events = [first, event, last]
    months = engine.timeline_month_input(events)
    long_text = '더불어민주당 초선 55명이 최민희 최고위원의 정치적 거취를 진지하게 판단하라며 징계 요구를 발표했다.'
    assert engine.validate_month_text(long_text, months[0]) == ['text 문자열 형식 위반']
    short_text = '초선 55명, 최민희에 “정치적 거취” 판단 요구'
    assert engine.validate_month_text(short_text, months[0]) == []
    unsupported = short_text.replace('55명', '56명').replace('정치적 거취', '즉시 결정')
    raw = {'months': [{'month': '2026-11', 'steps': ['여야 방송 정책 공방', unsupported, '의원 거취 논의 재개']}]}
    flow = engine.match_timeline_months(raw, months)['months']
    assert flow[0]['checks'] == ['근거 제목에 없는 숫자·단위: 56명', '그 달 근거에 없는 인용구']
    panel = app.render_timeline(dict(items=events, months=flow,
                                     model='qwen3:4b', llm_ms=1), [])
    assert all(reason in panel for reason in flow[0]['checks'])

    inputs = engine.timeline_summary_input([event])
    summary = short_text + ' 성명을 통해 의원의 거취를 신중히 판단하라고 촉구했다.'
    assert engine.validate_timeline_summary(summary, inputs, ['최민희']) == []
    invalid = unsupported + ' 성명의 내용을 다시 언급했다.'
    assert engine.validate_timeline_summary(invalid, inputs, ['최민희']) == [
        '입력 근거에 없는 숫자·단위: 56명', '입력 근거에 없는 인용구']


def test_sparse_month_always_uses_title_directions_in_date_order():
    events = [dict(date_from=day, date_to=day, representative={'title': title})
              for day, title in [('2026-10-28', '삼성전자 4% 급락'),
                                 ('2026-10-14', '삼성전자 반등 전망'),
                                 ('2026-10-02', '삼성전자 1.63% 하락 속 HLB글로벌 급등')]]
    month = engine.timeline_month_input(events)[0]
    assert engine.rule_month_flow(month, '삼성전자') == '10/2 1.63% 하락 → 10/28 4% 급락'
    assert engine.rule_month_flow(month, 'HLB글로벌') == '10/2 급등'
    sparse = engine.timeline_month_input([events[0], dict(events[1], representative={'title': '삼성전자 반등'})])
    matched = engine.match_timeline_months({'months': [{'month': '2026-10', 'steps': []}]}, sparse,
                                           price=True)['months'][0]
    assert (matched['text'], matched['source'], matched['checks']) == ('10/14 반등 → 10/28 4% 급락', '규칙', [])


def test_month_flow_shows_month_on_first_step_and_keeps_dates_for_repeated_period():
    june = ['러브버그 첫 출몰 신고', '도심 러브버그 확산', '방제 작업 시작']
    july = ['하천가 출몰 신고', '추가 방제 작업', '신고 건수 감소']
    dates = [('06-03', '06-04'), ('06-08', '06-08'), ('06-27', '06-27'),
             ('07-02', '07-02'), ('07-14', '07-14'), ('07-24', '07-24')]
    events = [dict(date_from=f'2026-{start}', date_to=f'2026-{end}', representative={'title': title})
              for (start, end), title in zip(dates, june + july)]
    raw = {'months': [{'month': '2026-06', 'steps': june}, {'month': '2026-07', 'steps': july}]}
    flows = engine.match_timeline_months(raw, engine.timeline_month_input(events))['months']
    assert [flow['checks'] for flow in flows] == [[], []]
    assert [flow['text'] for flow in flows] == [
        '6/3~4 러브버그 첫 출몰 신고 → 6/8 도심 러브버그 확산 → 말 방제 작업 시작',
        '7월 초 하천가 출몰 신고 → 중순 추가 방제 작업 → 말 신고 건수 감소',
    ]


def test_rule_flow_prefers_high_coverage_and_keeps_title_measurements_separate():
    titles = [
        (6, 6, 3, '삼성전자 1.63% 하락 속 HLB글로벌 19.99% 급등', True),
        (13, 16, 26, '[속보]코스피 5%대 급락, 매도 사이드카 발동', False),
        (15, 15, 7, '코스피-코스닥 8%대 폭락 서킷브레이커 20분간 거래 중단', False),
        (18, 19, 7, '삼성전자 금융위기급 폭락', True),
        (29, 30, 26, '삼성전자 영업익 106조인데 주가는 되레 4% 하락', True),
    ]
    events = [dict(date_from=f'2026-10-{start:02}', date_to=f'2026-10-{end:02}', article_count=count,
                   representative={'title': title}, aspect_subject_match=direct)
              for start, end, count, title, direct in titles]
    month = engine.timeline_month_input(events)[0]
    assert engine.rule_month_flow(month, '삼성전자') == (
        '10/6 1.63% 하락 → 10/13~16 코스피 5%대 급락·사이드카 → 10/29~30 영업익 106조인데 주가 4% 하락')
    assert engine.month_title_excerpt(titles[2][3], '삼성전자') == '코스피·코스닥 8%대 폭락·서킷브레이커'
    assert engine.month_title_excerpt('삼성전자 60,000원대 하락', '삼성전자') == '60,000원대 하락'

    steps = ['1.63% 하락', '5%대 급락·사이드카', '영업익 106조인데 주가 4% 하락']
    raw = {'months': [{'month': month['month'], 'steps': steps}]}
    flow = engine.match_timeline_months(raw, [month], aspect_subject='삼성전자')['months'][0]
    assert flow['source'] == 'AI' and ' → 중순 코스피 5%대 급락·사이드카' in flow['text']
    steps[2] = '영업익 106조인데 주가는 되레 4%'
    flow = engine.match_timeline_months(raw, [month], aspect_subject='삼성전자')['months'][0]
    assert flow['source'] == '규칙' and flow['checks'] == ['근거 제목의 방향 누락']
    steps[2] = '영업익 106조 4% 하락'
    flow = engine.match_timeline_months(raw, [month], aspect_subject='삼성전자')['months'][0]
    assert flow['source'] == '규칙' and flow['checks'] == ['실적과 주가의 구분 누락']
    steps[1] = '코스피 8%대 급락'
    flow = engine.match_timeline_months(raw, [month], aspect_subject='삼성전자')['months'][0]
    assert flow['source'] == '규칙' and flow['checks'] == ['근거 제목에 없는 숫자·단위: 8%']


def test_batch_only_writes_validated_summary_to_null():
    conn = Mock()
    conn.__enter__ = Mock(return_value=conn)
    conn.__exit__ = Mock(return_value=False)
    conn.execute.return_value.fetchall.return_value = [
        {'group_id': 1, 'article_id': 10, 'url': 'https://www.donga.com/news/1'},
        {'group_id': 2, 'article_id': None, 'url': None},
        {'group_id': 3, 'article_id': 30, 'url': 'https://www.donga.com/news/3'}]
    conn.execute.return_value.fetchone.return_value = {'article_id': 10}
    with patch.object(batch.psycopg, 'connect', return_value=conn), patch.object(
            engine, 'connection_options', return_value={}), patch.object(
            batch, 'summarize', side_effect=[('검증된 두 문장 요약이다. 기존 자료는 유지한다.', '성공'),
                                            (None, '요약 검사 실패')]):
        result = batch.run(3)
    updates = [call.args for call in conn.execute.call_args_list if call.args[0].startswith('UPDATE')]
    assert result['success'] == 1 and result['null'] == 2 and len(updates) == 1
    assert 'SET summary=%s' in updates[0][0] and 'summary IS NULL' in updates[0][0]
    assert updates[0][1][1:] == (10, 1, 1)
