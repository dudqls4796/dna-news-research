"""기업 미팅 질문의 날짜, 핵심 판정, 차트 및 응답 회귀를 검사한다."""

from collections import Counter, OrderedDict
from contextlib import nullcontext
from datetime import date
import json
from urllib.parse import urlencode
from unittest.mock import Mock
import re

import pytest

from search import app, engine


TODAY = date(2026, 11, 23)


@pytest.mark.parametrize(('ask', 'topical'), [
    ('12월 겨울 일본여행', True), ('3월 벚꽃 명소', True), ('10월 단풍 축제', True),
    ('5월 가볼 만한 곳', True), ('12월 크리스마스 데이트', True),
    ('12월 삼성전자 주가', False), ('9월 수출', False), ('2026년 3월 벚꽃 명소', False),
])
def test_seasonal_month_is_topic_not_period(ask, topical):
    assert engine.question_topic_month(ask) is topical


@pytest.mark.parametrize(('ask', 'dates', 'expected', 'topic'), [
    ('지난주 김도영', (None, None), ('2026-11-16', '2026-11-22'), '김도영'),
    ('12월 일본 여행', ('2026-12-01', '2026-12-31'), (None, None), '일본 여행 12월'),
    ('올해 최적의 겨울 여행 코스', (None, None), ('2026-01-01', '2026-11-23'), '최적의 겨울 여행 코스'),
    ('2026년 3월 1일부터 3월 15일까지 김도영', (None, None), ('2026-03-01', '2026-03-15'), '김도영'),
    ('작년 크리스마스', (None, None), ('2025-01-01', '2025-12-31'), '크리스마스'),
    ('2027년 1월 삼성전자 주가', ('2027-01-01', '2027-01-31'), (None, None), '삼성전자 주가'),
])
def test_meeting_dates_are_grounded_and_removed_from_topic(monkeypatch, ask, dates, expected, topic):
    infer_context = Mock(return_value=('김도영', '스포츠', 1))
    monkeypatch.setattr(engine, 'infer_person_context', infer_context)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    raw = {'keywords': [], 'q': ask, 'date_from': dates[0], 'date_to': dates[1], 'publisher': None}
    if ask.startswith('12월'):
        raw['q'] = '일본 여행'  # 모델이 월을 주제에서 누락해도 복원한다.
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    converted = engine.convert_question(ask, TODAY)
    assert (converted['date_from'], converted['date_to']) == expected
    assert converted['q'] == topic
    infer_context.assert_not_called()


@pytest.mark.parametrize(('ask', 'publisher', 'topic'), [
    ('스포츠조선 야구', '스포츠조선', '야구'),
    ('스포츠 조선에서 야구', '스포츠조선', '야구'),
    ('스포츠동아 디어워즈', '스포츠동아', '디어워즈'),
    ('중앙일보 부동산', '중앙일보', '부동산'),
    ('동아일보에서는 마라톤 대회', '동아일보', '마라톤 대회'),
    ('테스트 미디어의 마라톤 대회', '테스트미디어', '마라톤 대회'),
])
def test_question_ignores_publisher_even_if_model_proposes_it(monkeypatch, ask, publisher, topic):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [{'publisher': '테스트 미디어'}])
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    monkeypatch.setattr(engine, 'infer_person_context', lambda _: None)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    keyword = topic.split()[0]
    postprocess = engine.postprocess_question
    lookup = Mock(return_value={keyword: 5})
    monkeypatch.setattr(engine, 'postprocess_question', lambda raw: postprocess(raw, lookup))
    raw = {'keywords': [publisher] if keyword == '야구' else [publisher, keyword],
           'required_terms': [publisher, keyword], 'supporting_terms': [publisher],
           'subject_names': [publisher], 'q': ask, 'publisher': publisher}
    chat = Mock(return_value=(raw, 1))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    result = engine.convert_question(ask, TODAY)
    assert chat.call_args.args[1]['question'] == topic
    lookup.assert_called_once_with([] if keyword == '야구' else topic.split())
    assert (result['q'], result['keywords'], result['publisher']) == (topic, [keyword], None)
    assert result['required_terms'] == [keyword] and result['supporting_terms'] == []
    assert result['term_source'] == 'model'


@pytest.mark.parametrize(('phrase', 'topic', 'today', 'start', 'end'), [
    ('최근 일주일', '손흥민', date(2026, 11, 24), '2026-11-18', '2026-11-24'),
    ('최근 하루', '증시', date(2026, 11, 24), '2026-11-24', '2026-11-24'),
    ('지난 이틀', '증시', date(2026, 11, 24), '2026-11-23', '2026-11-24'),
    ('최근 사흘간', '증시', date(2026, 11, 24), '2026-11-22', '2026-11-24'),
    ('지난 나흘 동안', '증시', date(2026, 11, 24), '2026-11-21', '2026-11-24'),
    ('최근 닷새', '증시', date(2026, 11, 24), '2026-11-20', '2026-11-24'),
    ('지난 엿새', '증시', date(2026, 11, 24), '2026-11-19', '2026-11-24'),
    ('최근 이레', '증시', date(2026, 11, 24), '2026-11-18', '2026-11-24'),
    ('지난 열흘', '증시', date(2026, 11, 24), '2026-11-15', '2026-11-24'),
    ('지난 보름', '환율', date(2026, 11, 24), '2026-11-10', '2026-11-24'),
    ('최근 보름', '환율', date(2026, 11, 24), '2026-11-10', '2026-11-24'),
    ('지난 3일', '손흥민', date(2026, 11, 24), '2026-11-22', '2026-11-24'),
    ('최근 2주 동안', '손흥민', date(2026, 11, 24), '2026-11-11', '2026-11-24'),
    ('지난 한 달', '손흥민', date(2026, 3, 31), '2026-02-28', '2026-03-31'),
    ('최근 2개월간', '손흥민', date(2026, 1, 31), '2025-11-30', '2026-01-31'),
    ('올해 상반기', '수출', date(2026, 11, 24), '2026-01-01', '2026-06-30'),
    ('올해 하반기', '수출', date(2026, 11, 24), '2026-07-01', '2026-11-24'),
    ('작년 하반기', '수출', date(2026, 11, 24), '2025-07-01', '2025-12-31'),
    ('2024년 1분기', '수출', date(2026, 11, 24), '2024-01-01', '2024-03-31'),
    ('작년 2분기', '수출', date(2026, 11, 24), '2025-04-01', '2025-06-30'),
    ('2026년 3 분기', '수출', date(2026, 11, 24), '2026-07-01', '2026-09-30'),
    ('올해 4분기', '수출', date(2026, 11, 24), '2026-10-01', '2026-11-24'),
    ('2027년 상반기', '수출', date(2026, 11, 24), None, None),
])
def test_periods_override_model_dates_and_remove_date_fragments(monkeypatch, phrase, topic, today, start, end):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    lookup = Mock(return_value={topic: 5})
    monkeypatch.setattr(engine, 'postprocess_question', lambda raw: postprocess(raw, lookup))
    ask = f'{phrase} {topic}'
    raw = {'keywords': [*phrase.split(), topic], 'q': ask, 'required_terms': [topic],
           'supporting_terms': [phrase, *phrase.split()], 'date_from': '2026-01-01', 'date_to': '2026-12-31'}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    result = engine.convert_question(ask, today)
    lookup.assert_called_once_with([topic])
    assert (result['date_from'], result['date_to'], result['q']) == (start, end, topic)
    assert result['keywords'] == result['required_terms'] == [topic]
    assert result['supporting_terms'] == []


@pytest.mark.parametrize(('question', 'model_required', 'model_supporting', 'required', 'supporting'), [
    ('설 연휴 귀성길', [], [], ['귀성길'], ['설', '연휴']),
    ('하이브 뉴진스', ['하이브'], [], ['하이브', '뉴진스'], []),
    ('하이브 뉴진스', ['하이브'], ['뉴진스'], ['하이브', '뉴진스'], []),
])
def test_convert_recovers_query_subjects(monkeypatch, question, model_required, model_supporting, required, supporting):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'postprocess_question', lambda raw: postprocess(raw, lambda words: dict.fromkeys(words, 5)))
    raw = {'keywords': model_required, 'q': question, 'required_terms': model_required,
           'supporting_terms': model_supporting, 'subject_names': model_required}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    result = engine.convert_question(question, TODAY)
    assert result['required_terms'] == required and result['supporting_terms'] == supporting
    terms = engine.core_terms(result)
    assert list(terms) == required
    if len(required) > 1:
        title = '하이브와 뉴진스 관련 보도'
        assert all(re.search(pattern, title) for pattern, _ in terms.values())
        assert not re.search(terms['뉴진스'][0], '하이브 주가 반등')


@pytest.mark.parametrize(('question', 'keywords', 'date_range'), [
    ('CEO 시총', ['CEO', '시총'], (None, None)),
    ('시가총액 ceo', ['시가총액', 'ceo'], (None, None)),
    ('어제 CEO 실적 기사', ['CEO', '실적'], ('2026-11-22', '2026-11-22')),
])
def test_role_acronym_with_registered_topic_preserves_both_terms(monkeypatch, question, keywords, date_range):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    chat = Mock(side_effect=AssertionError('등록 관점어의 질문 해석에 모델이 필요하지 않음'))
    monkeypatch.setattr(engine, 'ollama_chat', chat)

    conversion = engine.convert_question(question, TODAY)

    assert conversion['keywords'] == conversion['required_terms'] == keywords
    assert conversion['person_names'] == conversion['supporting_terms'] == []
    assert (conversion['date_from'], conversion['date_to']) == date_range
    params = {**conversion, 'excluded_categories': engine.excluded_categories(conversion['q'])}
    assert engine.person_title_flag(params) == ('', ())
    chat.assert_not_called()


def test_role_acronym_uses_article_evidence_with_previously_resolved_query():
    params = engine.validate_params({'q': 'CEO 시총', 'keywords': ['시총']})
    params.update(required_terms=['시총'], supporting_terms=['CEO'], person_names=['시총'])
    terms = engine.core_terms(params)
    title = '엔비디아 시총 한 주 새 770000000000000 원 증발 단일 기업 역대 최대'

    assert set(terms) == {'시총', 'CEO'} and not any(required for _, required in terms.values())
    sql, values = engine.core_article_query(params, None, terms)
    assert 'UNION ALL SELECT article_id, false FROM article_keywords' in sql
    assert 'second_term.article_id IS NOT NULL' in sql
    assert sql.count('%s') == len(values) and ['CEO', 'ceo'] in values
    article = {'title': title, 'evidence_keywords': ['시총', 'CEO']}
    assert engine.article_match_reason(article, terms, []) == {'title': ['시총'], 'keywords': ['CEO'], 'basis': ''}
    assert engine.article_match_reason({'title': '시총 상승'}, terms, [])['keywords'] == []


def test_role_acronym_still_applies_title_context_to_person(monkeypatch):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    result = engine.convert_question('김도영 CEO', TODAY)
    assert result['keywords'] == result['required_terms'] == result['person_names'] == ['김도영']
    assert result['supporting_terms'] == ['CEO']

    params = {**result, 'excluded_categories': engine.excluded_categories(result['q'])}
    assert engine.core_terms(params)['김도영'][1]
    assert engine.person_title_flag(params)[0]


@pytest.fixture
def convert_with_model(monkeypatch):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'postprocess_question', lambda raw: postprocess(raw, lambda words: dict.fromkeys(words, 5)))

    def convert(question, *, today=TODAY, **raw):
        monkeypatch.setattr(engine, 'ollama_chat', lambda *_: ({'q': question, **raw}, 1))
        return engine.convert_question(question, today)

    return convert


@pytest.mark.parametrize(('ask', 'counts', 'model_term', 'model_support', 'required', 'supporting'), [
    ('테슬라 한국', {'테슬라': 1775, '한국': 50001}, '한국', ['테슬라'], '테슬라', '한국'),
    ('선거 FIFA', {'선거': 32494, 'FIFA': 2358}, '선거', ['FIFA'], 'FIFA', '선거'),
    ('선거 FIFA', {'선거': 32494, 'FIFA': 2358}, '선거', [], 'FIFA', '선거'),
    ('한국경제 전략적 서울', {'전략적': 377, '서울': 50001}, '서울', ['전략적'], '전략적', '서울'),
    ('대표 민주당', {'대표': 50001, '민주당': 31439}, '민주당', ['대표'], '민주당', '대표'),
    ('경찰 A씨', {'경찰': 48475, 'A씨': 23348}, '경찰', ['A씨'], '경찰', 'A씨'),
])
def test_short_question_prefers_specific_term_only_when_frequency_gap_is_large(
        monkeypatch, ask, counts, model_term, model_support, required, supporting):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    lookup = Mock(side_effect=lambda words: {word: counts[word] for word in words if word in counts})
    monkeypatch.setattr(engine, 'postprocess_question', lambda raw: postprocess(raw, lookup))
    raw = {'q': model_term, 'keywords': [model_term], 'required_terms': [model_term], 'supporting_terms': model_support}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    result = engine.convert_question(ask, TODAY)
    assert result['keywords'] == result['required_terms'] == [required]
    assert result['supporting_terms'] == [supporting]
    assert result['term_source'] == 'model' and list(engine.core_terms(result)) == [required]
    assert set(counts) <= set(lookup.call_args.args[0])


def test_police_agency_takes_precedence_over_general_crime_term(convert_with_model):
    result = convert_with_model('경찰청 범죄', q='범죄', keywords=['범죄'],
                                required_terms=['범죄'], supporting_terms=[])
    assert result['keywords'] == result['required_terms'] == ['경찰청']
    assert result['supporting_terms'] == ['범죄']
    assert list(engine.core_terms(result)) == ['경찰청']


def test_prompt_example_terms_cannot_override_question_fallback(convert_with_model):
    result = convert_with_model('올해 3분기 밝아 어느', q='삼성SDI 실적', keywords=['삼성SDI', '실적'],
                                required_terms=['삼성SDI', '실적'], supporting_terms=['매출'],
                                subject_names=['삼성SDI'])
    assert (result['date_from'], result['date_to']) == ('2026-07-01', '2026-09-30')
    assert result['q'] == '밝아 어느'
    assert result['required_terms'] == result['keywords'] == ['밝아']
    assert result['supporting_terms'] == [] and result['term_source'] == 'legacy'


@pytest.mark.parametrize('filler', ['어느', '어떤', '무슨', '이런', '그런', '저런', '모든', '여러', '각'])
@pytest.mark.parametrize('model_term', ['경찰', 'filler'])
def test_short_question_ignores_determiners_even_when_rare(monkeypatch, filler, model_term):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    counts = {'경찰': 48475, filler: 10}
    monkeypatch.setattr(engine, 'postprocess_question',
                        lambda raw: postprocess(raw, lambda words: {w: counts[w] for w in words if w in counts}))
    term = filler if model_term == 'filler' else model_term
    other = '경찰' if term == filler else filler
    raw = {'q': term, 'keywords': [term], 'required_terms': [term], 'supporting_terms': [other]}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    result = engine.convert_question(f'어제 경찰 {filler}', TODAY)
    assert result['required_terms'] == result['keywords'] == ['경찰']
    assert filler not in result['supporting_terms']


@pytest.mark.parametrize('filler', ['동안', '지금', '이제', '요즘', '당시', '때문', '통해', '위해', '대해', '관련'])
@pytest.mark.parametrize('topic', ['국회', '적발'])
@pytest.mark.parametrize('model_term', ['topic', 'filler'])
def test_short_question_ignores_time_and_function_words(monkeypatch, filler, topic, model_term):
    monkeypatch.setattr(engine, 'list_publishers', lambda: [])
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'postprocess_question',
                        lambda raw: postprocess(raw, lambda words: {w: 5 if w == filler else 40000 for w in words}))
    term = filler if model_term == 'filler' else topic
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: ({'q': term, 'keywords': [term],
                        'required_terms': [term], 'supporting_terms': []}, 1))
    result = engine.convert_question(f'{topic} {filler}', TODAY)
    assert result['required_terms'] == result['keywords'] == [topic]
    assert filler not in result['supporting_terms']


@pytest.mark.parametrize(('ask', 'start', 'end'), [
    ('적발 지난 이틀 동안', '2026-11-22', '2026-11-23'),
    ('국회 올해부터 지금까지', '2026-01-01', '2026-11-23'),
    ('국회 요즘 최근 3일', '2026-11-21', '2026-11-23'),
])
def test_time_stopwords_preserve_explicit_periods(convert_with_model, ask, start, end):
    topic = '적발' if ask.startswith('적발') else '국회'
    result = convert_with_model(ask, q=topic, keywords=[topic], required_terms=[topic], supporting_terms=[],
                                date_from=start, date_to=end)
    assert (result['date_from'], result['date_to']) == (start, end)
    assert result['keywords'] == result['required_terms'] == [topic]


@pytest.mark.parametrize('ask', ['국회 요즘', '국회 지금까지'])
def test_vague_time_words_alone_do_not_create_a_period(convert_with_model, ask):
    result = convert_with_model(ask, q='국회', keywords=['국회'], required_terms=['국회'],
                                date_from='2026-11-01', date_to='2026-11-23')
    assert (result['date_from'], result['date_to']) == (None, None)


def test_grounded_terms_survive_noise_and_allow_aliases_and_spacing(convert_with_model):
    result = convert_with_model('조선일보 지난 보름 한전 주 4일제 논의', keywords=['한국전력'],
                                required_terms=['한국 전력', '주4일제', '삼성SDI', '보름'],
                                supporting_terms=['논의', '조선일보', '실적'])
    assert result['required_terms'] == ['한국전력', '주4일제']
    assert result['supporting_terms'] == ['논의']
    assert result['term_source'] == 'model'


def test_registered_company_stays_required_when_model_marks_it_supporting(convert_with_model):
    result = convert_with_model('한국전력 적자', keywords=['적자'],
                                required_terms=['적자'], supporting_terms=['한국전력'])
    assert set(result['required_terms']) == {'한국전력', '적자'}
    assert '한국전력' not in result['supporting_terms']


def test_cyber_rekka_stays_core_when_model_requires_punishment(convert_with_model):
    result = convert_with_model('사이버 렉카 처벌', keywords=['처벌'],
                                required_terms=['처벌'], supporting_terms=['사이버 렉카'])
    assert result['required_terms'] == ['사이버렉카']
    assert result['supporting_terms'] == ['처벌']


@pytest.mark.parametrize(('topic', 'fragment'), [
    ('4일제', '일제'), ('3일장', '일장'), ('1인 가구', '인'), ('2차전지', '차전지'), ('4일', '일'),
])
def test_numeric_subject_does_not_ground_a_fabricated_date(convert_with_model, topic, fragment):
    result = convert_with_model(topic, q=fragment, keywords=[fragment], required_terms=[fragment],
                                date_from='2026-11-04', date_to='2026-11-04')
    assert result['q'] == topic
    assert result['date_from'] is result['date_to'] is None
    assert fragment not in result['required_terms'] and fragment not in result['keywords']


@pytest.mark.parametrize(('topic', 'model_topic'), [('주 4일제', '주 제'), ('주 4일제', '주4일제'), ('주4일제', '주')])
def test_four_day_workweek_recovers_one_required_term_and_spacing_alias(convert_with_model, topic, model_topic):
    result = convert_with_model(f'조선일보 {topic}', q=model_topic, keywords=['주'], required_terms=['주'],
                                supporting_terms=[], date_from='2026-11-04', date_to='2026-11-04')
    assert (result['q'], result['keywords'], result['required_terms']) == (topic, ['주4일제'], ['주4일제'])
    assert result['publisher'] is result['date_from'] is result['date_to'] is None
    terms = engine.core_terms(result)
    for title in ('주4일제 도입 논의', '주 4일제 추진', '기업들 주 4일제에 관심'):
        assert re.search(terms['주4일제'][0], title)
    assert not re.search(terms['주4일제'][0], '주 4일 근무하고 휴가')
    assert engine.core_keyword_forms('주4일제') == ['주 4일제', '주4일제']
    assert engine.validate_params({'keywords': '주 4일제'})['keywords'] == ['주4일제']


@pytest.mark.parametrize(('phrase', 'start', 'end'), [
    ('4일 전에', '2026-11-20', '2026-11-20'),
    ('지난 4일', '2026-11-21', '2026-11-24'),
    ('11월 4일의', '2026-11-04', '2026-11-04'),
    ('11월 4일부터 8일까지', '2026-11-04', '2026-11-08'),
])
def test_day_numbers_with_date_context_are_removed(convert_with_model, phrase, start, end):
    result = convert_with_model(f'{phrase} 2차전지', today=date(2026, 11, 24), keywords=['2차전지'])
    assert (result['q'], result['date_from'], result['date_to']) == ('2차전지', start, end)
    assert result['required_terms'] == ['2차전지']


@pytest.mark.parametrize(('phrase', 'topic', 'required'), [
    ('그저께', '사건', []), ('그제에', '사건을 알려줘', []), ('엊그제', '사건 뉴스', []),
    ('엊그제', '전세 사기', ['전세사기']),
])
def test_two_days_ago_routes_generic_requests_to_digest(convert_with_model, phrase, topic, required):
    result = convert_with_model(f'{phrase} {topic}', today=date(2026, 11, 24),
                                keywords=[phrase, '사건', *required], required_terms=[phrase, '사건', *required])
    assert (result['q'], result['date_from'], result['date_to']) == (topic, '2026-11-22', '2026-11-22')
    assert result['keywords'] == result['required_terms'] == required
    if not required:
        assert result['llm_ms'] == 0
        params = engine.validate_params({key: result[key] for key in ('q', 'keywords', 'date_from', 'date_to')})
        assert params['mode'] == 'digest'
        sql, values = engine.candidate_query(params, None)
        assert 'ORDER BY routine_title, publisher_count DESC, article_count DESC' in sql
        assert (params['date_from'], params['date_to']) == values[1:3] and topic not in values


def test_date_digest_ranks_more_publishers_first():
    daily = {'routine_title': False, 'short_title': False}
    events = [
        {'group_id': 1, 'publishers': ['A', 'A'], 'article_count': 10, 'score': None,
         'matches': [daily], 'latest_match': '2026-11-22'},
        {'group_id': 2, 'publishers': ['A', 'B'], 'article_count': 2, 'score': None,
         'matches': [daily], 'latest_match': '2026-11-22'},
    ]
    assert [event['group_id'] for event in engine.sort_result_events(events, 'digest')] == [2, 1]


@pytest.mark.parametrize('season', ['봄', '여름철', '연말', '연초'])
def test_season_and_moving_season_are_supporting_modifiers(convert_with_model, season):
    result = convert_with_model(f'{season} 이사철 전세', keywords=[season], required_terms=[season],
                                supporting_terms=['이사철', '전세'], subject_names=[season])
    assert result['keywords'] == result['required_terms'] == ['전세']
    assert set(result['supporting_terms']) == {season, '이사철'}
    assert set(engine.core_terms(result)) == {'전세'}
    assert result['date_from'] is result['date_to'] is None


@pytest.mark.parametrize(('phrase', 'today', 'start', 'end', 'fallback'), [
    ('이번 주말', date(2026, 11, 24), '2026-11-21', '2026-11-22', True),
    ('지난 주말에', date(2026, 11, 24), '2026-11-21', '2026-11-22', False),
    ('다음주말', date(2026, 11, 24), '2026-11-21', '2026-11-22', True),
    ('이번 주말', date(2026, 11, 28), '2026-11-28', '2026-11-29', False),
    ('다음 주말', date(2026, 11, 29), '2026-11-28', '2026-11-29', True),
])
def test_weekend_dates_override_model_and_remove_full_phrase(convert_with_model, phrase, today, start, end, fallback):
    result = convert_with_model(f'{phrase} 날씨', today=today, q='말 날씨', keywords=[phrase, '날씨'],
                                required_terms=[phrase, '날씨'], supporting_terms=[],
                                date_from='2026-11-23', date_to='2026-11-24')
    assert (result['q'], result['date_from'], result['date_to']) == ('날씨', start, end)
    assert result['keywords'] == result['required_terms'] == ['날씨']
    assert bool(result.get('date_notice')) == fallback
    if fallback:
        assert '가장 최근에 지난 주말' in result['date_notice']


@pytest.mark.parametrize(('question', 'keywords', 'model_required', 'supporting', 'expected', 'titles'), [
    ('사이버 렉카 처벌', ['사이버', '렉카'], ['사이버 렉카 처벌', '사이버', '렉카'], ['처벌'], '사이버렉카',
     ['사이버렉카에 대한 수사', '사이버 렉카 피해 확산']),
    ('사이버렉카 처벌', ['사이버렉카'], ['사이버렉카 처벌', '사이버렉카'], ['처벌'], '사이버렉카', ['사이버 렉카 논란']),
    ('군 가혹행위', ['가혹행위'], ['군 가혹행위', '가혹행위'], [], '가혹행위', ['병사 가혹행위 방치 의혹']),
    ('전세 사기', ['전세', '사기'], ['전세 사기'], [], '전세사기', ['전세사기 피해자 지원']),
    ('학교 폭력 사과 기사', ['폭력'], ['학교 폭력 사과'], ['사과'], '폭력', ['학교 폭력 피해 호소']),
])
def test_overlapping_query_phrases_do_not_become_core_conditions(
        convert_with_model, question, keywords, model_required, supporting, expected, titles):
    result = convert_with_model(question, keywords=keywords, required_terms=model_required,
                                supporting_terms=supporting, subject_names=model_required)
    assert result['required_terms'] == [expected]
    assert result['supporting_terms'] == supporting
    terms = engine.core_terms(result)
    assert list(terms) == [expected]
    assert all(re.search(terms[expected][0], title) for title in titles)
    if expected == '사이버렉카':
        assert result['keywords'] == ['사이버렉카']
        assert engine.core_keyword_forms(expected) == ['사이버 렉카', '사이버렉카']


def test_resolved_search_ignores_old_required_phrase_and_compound_parts():
    params = engine.validate_params({'keywords': ['사이버', '렉카'], 'q': '사이버 렉카 처벌'})
    params.update(required_terms=['사이버 렉카 처벌', '사이버', '렉카'], supporting_terms=['처벌'])
    assert list(engine.core_terms(params)) == ['사이버렉카']


@pytest.mark.parametrize(('name', 'short'), [
    ('한국전력', '한전'), ('현대자동차', '현대차'), ('삼성전자', '삼전'), ('SK텔레콤', 'SKT'),
    ('한국토지주택공사', 'LH'), ('국민건강보험공단', '건보공단'), ('더불어민주당', '민주당'),
    ('국민의힘', '국힘'), ('배달의민족', '배민'),
])
def test_company_aliases_match_titles_and_keywords_in_both_directions(name, short):
    for query, alias in ((name, short), (short, name)):
        params = engine.validate_params({'keywords': [query], 'q': f'{query} 적자'})
        params['required_terms'] = [query, '적자']
        terms = engine.core_terms(params)
        assert re.search(terms[query][0], f'{alias} 적자 확대', re.IGNORECASE)
        assert not re.search(terms[query][0], f'가짜{alias}회사 적자', re.IGNORECASE)
        evidence = engine.article_match_reason({'title': '적자 확대', 'evidence_keywords': [alias]}, terms, [])
        assert query in evidence['keywords']
        _, values = engine.keyword_source([query], params['q'])
        assert alias in values[0] and re.search(values[1], f'{alias} 적자', re.IGNORECASE)


def test_korea_electric_power_query_finds_abbreviated_title(convert_with_model):
    result = convert_with_model('한국전력 적자', keywords=['한국전력', '적자'],
                                required_terms=['한국전력', '적자'])
    terms = engine.core_terms(result)
    assert set(terms) == {'한국전력', '적자'}
    assert all(re.search(pattern, '한전 적자 확대', re.IGNORECASE) for pattern, _ in terms.values())


@pytest.mark.parametrize(('subject', 'affiliation'), [('하이브', '뉴진스'), ('뉴진스', '하이브')])
def test_company_and_group_in_relationship_query_stay_required(convert_with_model, subject, affiliation):
    result = convert_with_model('하이브 뉴진스', keywords=[subject], required_terms=[subject],
                                supporting_terms=[affiliation], affiliation_terms=[affiliation],
                                person_names=[subject, affiliation])
    assert set(result['required_terms']) == {'하이브', '뉴진스'}
    assert result['supporting_terms'] == result['person_names'] == []
    # 이전 결과의 분할을 포함한 월 재조회에서도 두 기관의 제목·키워드 근거를 모두 허용한다.
    params = {**result, 'person_names': [subject], 'date_from': '2026-06-01', 'date_to': '2026-06-30'}
    terms = engine.core_terms(params)
    title, keywords = '뉴진스 활동 관련 보도', ['하이브']
    evidence = engine.article_match_reason({'title': title, 'evidence_keywords': keywords}, terms, [])
    assert evidence['title'] == ['뉴진스'] and evidence['keywords'] == keywords
    sql, values = engine.core_article_query(params, None, terms)
    assert 'UNION ALL SELECT article_id, false FROM article_keywords' in sql
    assert sql.count('FROM article_keywords k WHERE k.article_id=a.id') == 1
    assert params['date_from'] in values and params['date_to'] in values


@pytest.mark.parametrize('keywords', [[], ['교육'], ['AI']])
def test_ai_education_restores_both_required_keywords_and_core_checks(convert_with_model, keywords):
    result = convert_with_model('중앙일보 AI 교육', keywords=keywords, required_terms=['교육'],
                                supporting_terms=['AI'], person_names=['AI'])
    assert result['q'] == 'AI 교육' and result['publisher'] is None
    assert set(result['keywords']) == set(result['required_terms']) == {'AI', '교육'}
    assert result['supporting_terms'] == result['person_names'] == []
    params = engine.validate_params({'keywords': result['keywords'], 'q': result['q']})
    params.update(required_terms=result['required_terms'], supporting_terms=result['supporting_terms'])
    terms = engine.core_terms(params)
    sql, values = engine.core_article_query(params, None, terms)
    assert 'UNION ALL SELECT article_id, false FROM article_keywords' in sql
    assert 'second_term.article_id IS NOT NULL' in sql
    assert ['AI', 'ai'] in values and ['교육'] in values
    assert all(re.search(pattern, '학교 AI 교육 지원', re.IGNORECASE) for pattern, _ in terms.values())
    assert not re.search(terms['AI'][0], '교육 훈련에 MAI 프로그램 지원', re.IGNORECASE)


@pytest.mark.parametrize('person_names', [['이승엽'], []])
def test_dismissal_is_required_with_the_person_even_when_demoted_by_model(convert_with_model, person_names):
    result = convert_with_model('이승엽 감독 경질', keywords=['이승엽'], required_terms=['이승엽'],
                                supporting_terms=['감독', '경질'], person_names=person_names)
    assert result['required_terms'] == ['이승엽', '경질'] and result['supporting_terms'] == ['감독']
    terms = engine.core_terms(result)
    assert terms['이승엽'][1] and terms['경질'][1]
    assert all(re.search(pattern, '이승엽 경질 발표') for pattern, _ in terms.values())
    assert not re.search(terms['경질'][0], '이승엽 감독의 경기 소감')
    assert not re.search(terms['이승엽'][0], '이승엽 절친 일본 요미우리 아베 감독 경질 위기')


@pytest.mark.parametrize(('title', 'keywords', 'expected'), [
    ('요미우리 아베 감독 경질 위기', ['이승엽', '경질'], ['경질']),
    ('이승엽 감독의 경기 소감', ['경질'], ['이승엽']),
    ('이승엽 절친 일본 요미우리 아베 감독, 딸 폭행해 긴급 체포 경질 위기', ['이승엽'], ['경질']),
    ("'이승엽 절친' 요미우리 아베 감독, 자녀 폭행 혐의 체포 경질 위기", [], ['경질']),
    ('경질된 이승엽 전 감독, 선수단에 작별 인사', [], ['이승엽', '경질']),
    ('이승엽 감독 경질 발표', [], ['이승엽', '경질']),
])
def test_person_aspect_core_requires_evidence_in_one_title(title, keywords, expected):
    params = {**engine.validate_params({'keywords': '이승엽', 'q': '이승엽 감독 경질'}),
              'person_names': ['이승엽'], 'required_terms': ['이승엽', '경질'], 'supporting_terms': ['감독']}
    terms = engine.core_terms(params)
    sql, values = engine.core_article_query(params, None, terms)
    assert 'WHERE a.title ~* %s AND a.title ~* %s' in sql
    assert 'article_keywords' not in sql and sql.count('%s') == len(values)
    evidence = engine.article_match_reason({'title': title, 'evidence_keywords': keywords}, terms, [])
    assert evidence['title'] == expected and evidence['keywords'] == []


@pytest.mark.parametrize('question', ['손흥민 이적', '손흥민의 이적'])
def test_transfer_core_exclusion_only_applies_to_the_persons_successor(convert_with_model, question):
    params = convert_with_model(question, keywords=['이적'], person_names=['손흥민'],
                                required_terms=['이적'], supporting_terms=[])
    assert params['required_terms'] == ['이적', '손흥민']
    terms = engine.core_terms(params)
    assert all(title_required for _, title_required in terms.values())
    sql, values = engine.core_article_query(params, None, terms)
    assert 'WHERE a.title ~* %s AND a.title ~* %s' in sql and 'article_keywords' not in sql
    assert 'a.title !~* %s' in sql
    exclusion = values[-1]
    for title in ('손흥민의 미국 이적 발표', 'LAFC, 손흥민 영입 발표', '손흥민이 새 팀과 계약'):
        assert all(re.search(pattern, title) for pattern, _ in terms.values())
        assert not re.search(exclusion, title)
    assert re.search(exclusion, '‘손흥민 후계자’ 존슨, 팰리스로 이적')


@pytest.mark.parametrize(('name', 'club'), [
    ('김민재', '바이에른'), ('이강인', 'PSG'), ('황희찬', '울버햄튼'),
])
def test_club_query_keeps_the_players_name_required(convert_with_model, name, club):
    params = convert_with_model(f'{name} {club}', keywords=[club], required_terms=[club],
                                supporting_terms=[name], person_names=[])
    terms = engine.core_terms(params)
    assert set(terms) == {name, club} and terms[name][1]


def test_model_partition_cannot_drop_drug_keyword_or_treat_occupation_as_person(monkeypatch, convert_with_model):
    conversion = convert_with_model('마약 투약 연예인', keywords=['마약'], required_terms=['연예인'],
                                    supporting_terms=['투약'], person_names=['연예인'], subject_names=['연예인'])
    assert set(conversion['required_terms']) == {'마약', '투약'}
    assert conversion['supporting_terms'] == ['연예인']
    assert conversion['person_names'] == []
    params = engine.validate_params({field: conversion[field] for field in ('keywords', 'q')})
    params.update({field: conversion[field] for field in ('required_terms', 'supporting_terms', 'person_names')})
    day = '2026-11-24'
    titles = [
        {'id': 1, 'title': '유명 배우 마약 투약 혐의'},
        {'id': 2, 'title': '연예인 출신 정치인들'},
        {'id': 3, 'title': '유명 배우 투약 혐의', 'evidence_keywords': ['마약']},
    ]
    rows = [dict(id=i, group_id=8 if i < 3 else None, score=.99, published_date=day) for i in (1, 2, 3)]
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': [{'id': 1, 'title_core': True}, {'id': 3, 'title_core': False}]}),
        Mock(**{'fetchall.return_value': [{'article_id': 3}]}),
        Mock(**{'fetchall.return_value': rows[:1] + rows[2:]}),
        Mock(**{'fetchall.return_value': rows[1:2]}),
        Mock(**{'fetchone.return_value': {'monthly': []}}),
        Mock(**{'fetchall.return_value': [dict(group_id=8, article_count=2, date_from=day, date_to=day,
                                               representative_id=2)]}),
        Mock(**{'fetchall.return_value': [dict(item, published_date=day, publisher='동아일보') for item in titles]}),
        Mock(**{'fetchall.return_value': []}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    result = engine._search_db(conn, params, '[1]')
    core_sql, core_values = conn.execute.call_args_list[0].args
    assert 'a.title ~* %s OR second_term.article_id IS NOT NULL' in core_sql and ['마약'] in core_values
    title_pattern = engine.core_terms(params)['마약'][0]
    assert title_pattern in core_values
    assert re.search(title_pattern, titles[0]['title']) and not re.search(title_pattern, titles[1]['title'])
    core = {event['representative_id']: event['representative']['match_reason'] for event in result['events']}
    assert set(core) == {1, 3}
    assert '마약' in core[1]['title'] and '마약' in core[3]['keywords']


@pytest.mark.parametrize('partition', [{}, {'required_terms': []}, {'required_terms': ['투약']}])
def test_required_keyword_survives_missing_or_incomplete_partition(partition):
    params = {**engine.validate_params({'keywords': '마약', 'q': '마약 투약'}), **partition}
    terms = engine.core_terms(params)
    assert set(terms) == {'마약', '투약'}
    assert engine.article_match_reason({'title': '투약 혐의', 'evidence_keywords': ['마약']}, terms, []) == {
        'title': ['투약'], 'keywords': ['마약'], 'basis': ''}


@pytest.mark.parametrize(('question', 'affiliation', 'subject', 'aspect', 'people', 'title'), [
    ('에스파 카리나 열애', '에스파', '카리나', '열애', ['카리나'], '카리나, 열애 인정'),
    ('오픈AI ChatGPT 업데이트', '오픈AI', 'ChatGPT', '업데이트', [], 'ChatGPT 업데이트 공개'),
])
def test_affiliation_is_supporting_even_if_model_also_lists_it_as_a_subject(
        convert_with_model, question, affiliation, subject, aspect, people, title):
    conversion = convert_with_model(question, keywords=[affiliation, subject],
                                    required_terms=[affiliation, subject, aspect], supporting_terms=[],
                                    person_names=people, subject_names=[affiliation, subject],
                                    affiliation_terms=[affiliation])
    assert conversion['required_terms'] == [subject, aspect]
    assert conversion['supporting_terms'] == [affiliation]
    assert conversion['keywords'] == [subject]
    terms = engine.core_terms(conversion)
    assert list(terms) == [subject, aspect]
    assert terms[subject][1] == bool(people)
    assert all(re.search(pattern, title) for pattern, _ in terms.values())
    sql, values = engine.core_article_query(conversion, None, terms)
    assert sql.count('%s') == len(values) and not any(affiliation in str(value) for value in values)
    if people:
        reason = engine.article_match_reason({'title': '솔로 앨범 공개', 'evidence_keywords': [subject]}, terms, [])
        assert subject not in reason['keywords']


@pytest.mark.parametrize(('question', 'name', 'role', 'aspect'), [
    ('안철수 의원', '안철수', '의원', None),
    ('한동훈 대표', '한동훈', '대표', None),
    ('최태원 회장 이혼', '최태원', '회장', '이혼'),
    ('박찬욱 감독 칸', '박찬욱', '감독', '칸'),
    ('김연경 선수 부상', '김연경', '선수', '부상'),
])
def test_person_title_is_supporting_in_term_and_sql_filters(convert_with_model, question, name, role, aspect):
    subject = f'{name} {role}'
    conversion = convert_with_model(question, keywords=[subject], required_terms=[subject, *([aspect] if aspect else [])],
                                    supporting_terms=[], person_names=[name], subject_names=[subject])
    assert conversion['keywords'] == conversion['person_names'] == [name]
    assert conversion['supporting_terms'] == [role]
    params = {**conversion, 'excluded_categories': engine.excluded_categories(question)}
    terms = engine.core_terms(params)
    assert set(terms) == {name, *([aspect] if aspect else [])}
    assert terms[name][1]
    title = f'{name}, {aspect or "기자회견"} 관련 입장 공개'
    assert all(re.search(pattern, title) for pattern, _ in terms.values())
    sql, values = engine.core_article_query(params, None, terms)
    assert sql.count('%s') == len(values)
    assert all(role not in str(value) for value in values)


def test_member_wins_even_when_model_omits_affiliation(convert_with_model):
    conversion = convert_with_model('블랙핑크 제니 솔로', keywords=['블랙핑크', '제니'],
                                    required_terms=['블랙핑크', '제니', '솔로'], supporting_terms=[],
                                    subject_names=['블랙핑크', '제니'], person_names=['제니'], affiliation_terms=[])
    assert conversion['keywords'] == conversion['person_names'] == ['제니']
    assert conversion['required_terms'] == ['제니']
    assert set(conversion['supporting_terms']) == {'블랙핑크', '솔로'}
    for params in (conversion, {**conversion, 'required_terms': ['제니', '솔로'], 'supporting_terms': ['블랙핑크']}):
        terms = engine.core_terms(params)
        assert set(terms) == {'제니'} and terms['제니'][1]
        assert re.search(terms['제니'][0], '제니, 새 앨범 공개')


def test_member_and_dating_recovered_from_group_only_conversion(convert_with_model):
    result = convert_with_model('에스파 카리나 열애', q='에스파', keywords=['에스파'], required_terms=['에스파'],
                                supporting_terms=[], person_names=['에스파'], subject_names=['에스파'])
    assert result['required_terms'] == ['카리나', '열애']
    assert result['supporting_terms'] == ['에스파'] and result['person_names'] == ['카리나']
    terms = engine.core_terms(result)
    assert all(title_required for _, title_required in terms.values())
    evidence = engine.article_match_reason(
        {'title': '카리나, 새 앨범 공개', 'evidence_keywords': ['열애']}, terms, result['supporting_terms'])
    assert evidence['title'] == ['카리나'] and evidence['keywords'] == []


def test_crude_oil_title_ignores_the_known_false_compound(convert_with_model):
    result = convert_with_model('지난 열흘 유가', today=date(2026, 11, 24), keywords=['유가'])
    assert result['required_terms'] == ['유가'] and result['date_from'] == '2026-11-15'
    terms = engine.core_terms(result)
    for title in ('국제유가 상승', '고유가 지속', '유가가 배럴당 80달러', '유가사지론과 국제유가'):
        assert re.search(terms['유가'][0], title)
    for title in ('불교 문헌 유가사지론', '유가 사지론 번역'):
        evidence = engine.article_match_reason({'title': title, 'evidence_keywords': ['유가사지론']}, terms, [])
        assert evidence['title'] == evidence['keywords'] == []


@pytest.mark.parametrize(('group', 'member'), [('방탄소년단', '진'), ('BTS', '뷔')])
def test_one_syllable_member_and_military_discharge_recovered(convert_with_model, group, member):
    conversion = convert_with_model(f'{group} {member} 전역', q=f'{group} 전역', keywords=[group],
                                    required_terms=[group], supporting_terms=['전역'],
                                    subject_names=[group], person_names=[group], affiliation_terms=[group])
    assert conversion['keywords'] == conversion['person_names'] == [member]
    assert conversion['required_terms'] == [member, '전역']
    assert conversion['supporting_terms'] == [group]
    terms = engine.core_terms(conversion)
    for title in (f'{member}, 전역 소감 전해', f'{member}이 전역', f'{group} {member} 전역'):
        assert all(re.search(pattern, title) for pattern, _ in terms.values())
    for title in ('남미 전역 기록', '서울 전역, 교통 통제', '사진 공개, 군 전역 후 첫 활동',
                  '인제대, 진로 부담 낮춘다', f'{member}, 인제대 방문', f'{member}, 서울 전역을 돌며 공연'):
        assert not all(re.search(pattern, title) for pattern, _ in terms.values())
    reason = engine.article_match_reason({'title': '서울 전역 교통 통제', 'evidence_keywords': [member, '전역']}, terms, [])
    assert reason['title'] == reason['keywords'] == []
    params = engine.validate_params({'keywords': conversion['keywords'], 'q': conversion['q']}) | conversion
    sql, values = engine.candidate_query(params, '[1]', [1, 2])
    assert '(a.title ~* %s) AS partial_match' in sql and terms[member][0] in values


def test_one_syllable_member_is_not_extracted_from_a_longer_query_word(convert_with_model):
    conversion = convert_with_model('방탄소년단 진로', keywords=['방탄소년단'],
                                    required_terms=['방탄소년단', '진로'], supporting_terms=[], person_names=['진'])
    assert conversion['person_names'] == []
    assert conversion['required_terms'] == ['방탄소년단', '진로']


@pytest.mark.parametrize(('question', 'raw', 'required', 'supporting', 'title', 'unrelated'), [
    ('해병대 수사 외압', dict(keywords=['해병대'], required_terms=['해병대'], supporting_terms=['수사'],
                          person_names=['해병대'], affiliation_terms=['해병대']),
     ['해병대', '외압'], ['수사'], '해병대 수사 외압 의혹', '해병대 총기사고'),
    ('롯데 유통 구조조정', dict(keywords=['롯데 유통'], required_terms=['롯데 유통'], supporting_terms=['구조조정'],
                            subject_names=['롯데 유통'], affiliation_terms=['롯데 유통']),
     ['롯데', '구조조정'], ['유통'], '롯데, 구조 조정 계획 발표', '롯데 유통 사업 확장'),
    ('프로야구 경기 일정', dict(keywords=['프로야구'], required_terms=['프로야구'], supporting_terms=['일정']),
     ['프로야구', '일정'], [], '프로야구 개막 일정', '프로야구 경기 결과'),
])
def test_concrete_aspects_replace_generic_context_in_core_judgment(
        convert_with_model, question, raw, required, supporting, title, unrelated):
    conversion = convert_with_model(question, **raw)
    assert conversion['keywords'] == required[:1]
    assert conversion['required_terms'] == required and conversion['supporting_terms'] == supporting
    assert conversion['person_names'] == []
    terms = engine.core_terms(conversion)
    assert list(terms) == required
    assert all(re.search(pattern, title) for pattern, _ in terms.values())
    assert not re.search(terms[required[1]][0], unrelated)


def test_exam_question_drops_generic_action_even_if_model_requires_it(convert_with_model):
    conversion = convert_with_model('수능 끝나고 할 일', keywords=['수능', '할 일', '끝나고'],
                                    required_terms=['할 일', '끝나고'], supporting_terms=['수능'])
    assert conversion['keywords'] == conversion['required_terms'] == ['수능']
    assert conversion['supporting_terms'] == []
    for params in (conversion, {'keywords': [], 'q': conversion['q']}):
        terms = engine.core_terms(params)
        assert list(terms) == ['수능']
        assert not re.search(terms['수능'][0], '예능 새싹에서 방송 기계로 “할 일 다했다”')
        assert re.search(terms['수능'][0], '수능 후에 수험생이 듣고 싶은 소식')


@pytest.mark.parametrize('period', ['시즌', '철', '기간'])
def test_graduation_flower_price_recovers_concrete_aspect(convert_with_model, period):
    conversion = convert_with_model(f'졸업 {period} 꽃값', keywords=['졸업', period],
                                    required_terms=[f'졸업 {period}'], supporting_terms=['꽃값'])
    assert conversion['keywords'] == ['졸업']
    assert conversion['required_terms'] == ['졸업', '꽃값']
    assert conversion['supporting_terms'] == [period]
    terms = engine.core_terms(conversion)
    assert re.search(terms['꽃값'][0], '졸업식 앞두고 꽃 가격 급등')
    assert not re.search(terms['꽃값'][0], f'졸업 {period} 알바 모집')


@pytest.mark.parametrize('has_core', [True, False])
def test_empty_required_partition_filters_results_and_chart(monkeypatch, has_core):
    q = '설 연휴 귀성길' if has_core else '설 연휴 귀성길 정체'
    params = {**engine.validate_params({'q': q}), 'required_terms': [], 'supporting_terms': []}
    day = '2026-11-24'
    rows = [dict(id=1, group_id=None, score=.94, published_date=day, partial_match=True),
            dict(id=2, group_id=None, score=.84, published_date=day, partial_match=False)]
    core_rows = [{'id': 1, 'title_core': True}] if has_core else []
    candidate_rows = Mock(**{'fetchall.return_value': rows})
    monthly = [{'month': '2026-11', 'count': 1, 'top_keywords': []}]
    article = dict(id=1, published_date=day, publisher='동아일보', title='고속도로 귀성길')
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': core_rows}),
        *([] if has_core else [Mock(**{'fetchall.return_value': [{'id': 1}]})]),
        candidate_rows,
        *([candidate_rows, candidate_rows] if has_core else [candidate_rows]),
        Mock(**{'fetchone.return_value': {'monthly': monthly}}),
        Mock(**{'fetchall.return_value': [article]}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    result = engine._search_db(conn, params, '[1]')
    core_sql, core_values = conn.execute.call_args_list[0].args
    title_pattern = engine.core_terms(params)['귀성길'][0]
    assert 'a.title ~* %s' in core_sql and title_pattern in core_values and re.search(title_pattern, article['title'])
    assert 'AS partial_match' in conn.execute.call_args_list[1 if has_core else 2].args[0]
    stats_sql, stats_values = conn.execute.call_args_list[-2].args
    assert 'FROM unnest(%s::bigint[])' in stats_sql and stats_values[0] == [1]
    assert result['stats']['monthly'] == monthly
    assert result['stats']['core_terms'] == (['귀성길'] if has_core else ['귀성길', '정체'])
    events = result['events'] if has_core else result['related_events']
    assert len(events) == 1 and events[0]['representative_id'] == 1
    assert result['stats']['basis'] == ('핵심 결과 판정을 통과한 전체 기사' if has_core else 'related')


@pytest.mark.parametrize(('keyword', 'title', 'unrelated'), [
    ('디어워즈', '연말 디 어워즈 수상자 발표', '연말 영화 어워즈 수상자 발표'),
    ('수상자발표', '연말 수상자\t발표', '연말 수상자 후보 발표'),
])
def test_required_keywords_match_spaced_titles(keyword, title, unrelated):
    params = engine.validate_params({'keywords': keyword})
    terms = engine.core_terms(params)
    sql, values = engine.core_article_query(params, None, terms)
    *_, anchor, keyword_forms = values
    assert anchor == terms[keyword][0] and keyword in keyword_forms
    assert re.search(anchor, title) and not re.search(anchor, unrelated)
    assert sql.count('FROM articles a WHERE') == 1
    assert 'bool_or(title_core)' in sql and 'UNION ALL' in sql
    assert sql.count('%s') == len(values)


@pytest.mark.parametrize(('word', 'matches', 'misses'), [
    ('파테크', ('파테크 열풍', '“파 테크” 관심', '파 테크로 절약', '파테크처럼 절약', '파테크에서도 효과'),
     ('DS단석, 청두 카이파 테크놀로지와 전력 사업 협력', '카이파 테크 협력', '파 테크놀로지 협력',
      '카이파테크 협력', '파 테크와이드 투자', 'A파 테크 투자')),
    ('사이버렉카', ('사이버렉카 처벌', '사이버 렉카에 대한 대책'), ('유사이버 렉카', '사이버 렉카운트')),
    ('주4일제', ('주4일제 논의', '기업들 주 4일제에 관심'), ('매주 4일제 논의', '주 4일제도권 논의')),
    ('꽃값', ('졸업식 꽃값이 올랐다', '시장 꽃 가격 상승'), ('목화꽃 값 추이', '꽃 가격표 부착')),
    ('김도영', ('이것이 김도영이다', '김도영이냐, 곽빈이냐', '김도영에겐 약속의 8회'), ('김도영급 선수',)),
])
def test_spacing_insensitive_title_patterns_require_word_boundaries(word, matches, misses):
    terms = engine.core_terms(engine.validate_params({'keywords': word}))
    for pattern in (terms[word][0], engine.supporting_title_pattern(word)):
        assert all(re.search(pattern, title) for title in matches)
        assert all(not re.search(pattern, title) for title in misses)


def test_unknown_compound_falls_back_to_distinctive_title_term_for_related_only(monkeypatch):
    word = '중급닌자시험'
    params = {**engine.validate_params({'q': word}), 'required_terms': [word]}
    day = '2026-11-24'
    title = '닌자 시험을 치르는 참가자'
    unrelated = '중등 교사 임용 시험 중급 과정'
    article = dict(id=1, title=title, published_date=day, publisher='동아일보')
    row = dict(id=1, group_id=None, published_date=day, score=.60, partial_match=True)
    counts = [dict(word=part, n=count) for part, count in [('중급', 49), ('닌자', 16), ('시험', 7936), ('자시', 6)]]
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': []}),
        Mock(**{'fetchall.return_value': counts}),
        Mock(**{'fetchall.return_value': [{'id': 1}]}),
        Mock(**{'fetchall.return_value': [row]}),
        Mock(**{'fetchone.return_value': {'monthly': [{'month': '2026-11', 'count': 1}]}}),
        Mock(**{'fetchall.return_value': [article]}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})

    result = engine._search_db(conn, params, '[1]')

    assert not result['events'] and result['stats']['core_terms'] == [word]
    related, = result['related_events']
    assert related['representative']['match_reason']['title'] == ['닌자']
    assert result['stats']['basis'] == 'related'
    _, core_values = conn.execute.call_args_list[0].args
    core_pattern = engine.core_terms(params)[word][0]
    assert core_pattern in core_values
    assert re.search(core_pattern, '중급 닌자 시험에 도전') and not re.search(core_pattern, title)
    _, (pieces,) = conn.execute.call_args_list[1].args
    assert all(len(part) >= 2 and part in word for part in pieces)
    related_sql, (related_pattern,) = conn.execute.call_args_list[2].args
    assert 'article_keywords' not in related_sql
    assert re.search(related_pattern, title) and not re.search(related_pattern, unrelated)
    candidate_sql, candidate_values = conn.execute.call_args_list[3].args
    assert 'AS partial_match' in candidate_sql and related_pattern in candidate_values
    assert [1] in candidate_values and conn.execute.call_count == 6


@pytest.mark.parametrize('word', ['파테크', '사이버렉카', '12월 겨울 일본여행'])
def test_compound_fallback_skips_short_registered_and_spaced_questions(word):
    params = engine.validate_params({'q': word})
    conn = Mock()
    assert not engine.compound_related_terms(conn, params, {word: ('', False)})
    conn.execute.assert_not_called()


def test_core_terms_keep_season_noun_and_require_person_title():
    params = engine.validate_params({'keywords': '솔로', 'q': '솔로를 위한 크리스마스'})
    assert set(engine.core_terms(params)) == {'솔로', '크리스마스'}
    params = engine.validate_params({'keywords': '김도영', 'date_from': '2026-03-01', 'date_to': '2026-03-15'})
    terms = engine.core_terms(params)
    assert terms['김도영'][1] is True
    sql, values = engine.core_article_query(params, None, terms)
    assert 'a.title ~* %s' in sql and 'article_keywords' not in sql
    assert sql.count('%s') == len(values) and '2026-03-15' in values
    terms = engine.core_terms(engine.validate_params({'keywords': '일본', 'q': '일본 여행 12월'}))
    assert re.search(terms['12월'][0], '12월 일본에서 가볼 만한 여행지')
    assert not re.search(terms['12월'][0], '112월')


@pytest.mark.parametrize('question', ['대표 갈등', '트럼프 중국'])
def test_core_query_checks_two_common_terms_after_candidate_id_lookup(question):
    params = engine.validate_params({'keywords': question, 'q': question})
    params.update(required_terms=question.split(), _duplicate_article_ids=[65, 66])
    terms = engine.core_terms(params)
    sql, values = engine.core_article_query(params, None, terms)

    assert list(terms) == question.split()
    assert 'FROM possible p LEFT JOIN' in sql
    assert 'SELECT DISTINCT article_id FROM article_keywords WHERE keyword=ANY' in sql
    assert 'FROM articles WHERE id=p.id OFFSET 0' in sql
    assert 'bool_or(anchor_title) AS anchor_title' in sql
    assert 'GROUP BY id ORDER BY id' in sql
    assert 'p.anchor_title AND a.title ~* %s' in sql
    assert 'second_term.article_id IS NOT NULL' in sql
    assert 'k.article_id=a.id' not in sql
    assert 'a.id NOT IN (SELECT unnest(%s::bigint[]))' in sql
    assert values[-1] == [65, 66] and sql.count('%s') == len(values)


@pytest.mark.parametrize('score', [.52, None])
def test_surname_honorific_accepts_exact_keyword_and_rejects_semantic_only_results(
        monkeypatch, convert_with_model, score):
    conversion = convert_with_model('아내 강씨', keywords=['강씨'], required_terms=['강씨'],
                                    supporting_terms=['아내'], person_names=['강씨'])
    assert conversion['keywords'] == conversion['required_terms'] == conversion['person_names'] == ['강씨']
    assert conversion['supporting_terms'] == ['아내']
    params = engine.validate_params({key: conversion[key] for key in ('q', 'keywords')})
    params.update({key: conversion[key] for key in ('required_terms', 'supporting_terms', 'person_names')})
    day = '2026-11-15'
    article = dict(id=762344, title='“아내가 나를 죽이려고 해서 죽였다” 자수한 80대男 구속심사',
                   published_date=day, publisher='문화일보', evidence_keywords=['강씨', '아내'])
    unrelated_titles = ('강서구 신청사로 이사 갑니다', '남편[내가 만난 명문장/강보라]', '[부고] 강성범(개그맨)씨 부친상',
                        '문강 씨가 발표했다')
    row = dict(id=article['id'], group_id=None, score=score, published_date=day, support_matches=1)
    semantic = [dict(id=i, group_id=None, score=.99, published_date=day, partial_match=False)
                for i, _ in enumerate(unrelated_titles, 1)]
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': [{'id': article['id'], 'title_core': False}]}),
        Mock(**{'fetchall.return_value': [{'article_id': article['id']}]}),
        Mock(**{'fetchall.return_value': [row]}),
        Mock(**{'fetchall.return_value': semantic}),
        Mock(**{'fetchone.return_value': {'monthly': [{'month': '2026-11', 'count': 1}]}}),
        Mock(**{'fetchall.return_value': [article]}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    result = engine._search_db(conn, params, '[1]')
    core_sql, (*_, title_pattern, exact_keywords) = conn.execute.call_args_list[0].args
    assert 'article_keywords WHERE keyword=ANY(%s::text[])' in core_sql
    assert exact_keywords == ['강씨']
    assert re.search(title_pattern, '강 씨의 진술') and re.search(title_pattern, '강씨가 진술했다')
    assert all(not re.search(title_pattern, title) for title in (article['title'], *unrelated_titles))
    semantic_sql, semantic_values = conn.execute.call_args_list[3].args
    assert 'AS partial_match' in semantic_sql and title_pattern in semantic_values
    core, = result['events']
    assert core['tier'] == 'core' and core['representative_id'] == 762344
    assert core['representative']['match_reason'] == {'title': ['아내'], 'keywords': ['강씨'], 'basis': ''}
    assert result['excluded_candidate_count'] == len(unrelated_titles)
    assert result['related_events'] == []


def test_core_chart_uses_all_passed_articles_before_event_limit(monkeypatch):
    params = engine.validate_params({'keywords': '김도영', 'date_from': '2026-03-01', 'date_to': '2026-03-15', 'limit': 1})
    day = '2026-03-15'
    rows = [
        dict(id=2, group_id=8, score=None, published_date=day),
        dict(id=1, group_id=8, score=None, published_date='2026-03-01'),
        dict(id=5, group_id=None, score=None, published_date='2026-03-01'),
    ]
    articles = [dict(id=i, published_date=day, publisher='동아일보', title=title)
                for i, title in ((1, '김도영, 복귀'), (2, '인턴십 발표'), (5, '시상식 소식'))]
    conn = Mock()
    core_ids = {1: True, 3: False, 4: True}
    expected_ids = list(core_ids)
    monthly = [{'month': '2026-03', 'count': len(expected_ids),
                'top_keywords': [{'keyword': '복귀', 'mention_sum': 3, 'article_count': 2, 'lift': None}]}]
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': [{'id': i, 'title_core': value} for i, value in core_ids.items()]}),
        Mock(**{'fetchall.return_value': [{'article_id': i} for i in (1, 2, 5)]}),
        Mock(**{'fetchall.return_value': rows}),
        Mock(**{'fetchone.return_value': {'monthly': monthly}}),
        Mock(**{'fetchall.return_value': [dict(group_id=8, article_count=2, date_from='2026-03-01',
                                               date_to=day, representative_id=2, publishers=['동아일보'])]}),
        Mock(**{'fetchall.return_value': articles}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    result = engine._search_db(conn, params, None)
    stats_sql, stats_values = conn.execute.call_args_list[3].args
    assert stats_values[0] == expected_ids and 'FROM unnest(%s::bigint[])' in stats_sql
    assert result['stats']['monthly'] == monthly
    assert sum(month['count'] for month in result['stats']['monthly']) == 3
    assert result['stats']['basis'] == '핵심 결과 판정을 통과한 전체 기사'
    core, = result['events']
    assert core['representative_id'] == 1
    assert core['tier'] == 'core'
    assert [row['tier'] for row in core['matches']] == ['core']
    assert core['representative']['match_reason']['title'] == ['김도영']
    assert result['related_events'] == []


def test_date_window_with_no_eligible_articles_exposes_nearest_and_zero_chart(monkeypatch):
    params = engine.validate_params({'q': '도리도항', 'keywords': '도리도항',
                                     'date_from': '2026-11-18', 'date_to': '2026-11-24', 'publisher': '동아일보'})
    params['required_terms'] = ['도리도항']
    params['_duplicate_article_ids'] = []
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: [])
    day = '2026-11-24'
    junk = [dict(id=i, group_id=None, score=.80, published_date=day, partial_match=False)
            for i in range(1, engine.VECTOR_CANDIDATES + 1)]
    nearest = [dict(id=i, published_date=date(2026, 11, published), publisher='동아일보',
                    title='도리도항 정비 소식', url=f'https://example.com/{i}')
               for i, published in ((321, 12), (322, 11))]
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': []}),
        Mock(**{'fetchall.return_value': []}),
        Mock(**{'fetchall.return_value': []}),
        Mock(**{'fetchall.return_value': junk}),
        Mock(**{'fetchall.return_value': nearest}),
        Mock(**{'fetchone.return_value': {'monthly': []}}),
    ]
    result = engine._json_values(engine._search_db(conn, params, '[1]'))
    assert result['candidate_count'] == result['excluded_candidate_count'] == 200
    assert result['events'] == result['related_events'] == result['stats']['monthly'] == []
    assert result['data_absence_message'] == "선택한 기간(11/18~11/24)에는 '도리도항' 기사가 없습니다. 가장 가까운 기사: 2026-11-12"
    assert [(item['id'], item['published_date']) for item in result['nearest_events']] == [
        (321, '2026-11-12'), (322, '2026-11-11')]
    _, lexical_values = conn.execute.call_args_list[1].args
    pattern = lexical_values[0]
    assert re.search(pattern, '도리 도항에 정비 사업')
    assert all(not re.search(pattern, title) for title in ('혁명의 결실을 거두는 도리', '도루묵 숯불구이'))
    nearest_sql, nearest_values = conn.execute.call_args_list[4].args
    assert 'bounds.first_day - a.published_date' in nearest_sql and 'LIMIT 5' in nearest_sql
    assert 'older.published_date=a.published_date AND older.id<a.id' in nearest_sql
    assert 'older_source.publisher IS NOT DISTINCT FROM s.publisher' in nearest_sql
    assert '동아일보' in nearest_values and nearest_values[-2:] == ('2026-11-18', '2026-11-24')
    assert conn.execute.call_args_list[5].args[1][0] == []
    html = app.render_page({'ask': '최근 일주일 도리도항'}, {**result, 'params': params, 'elapsed_ms': 0})
    assert '가장 가까운 기사: 2026-11-12' in html and 'href="https://example.com/321"' in html


@pytest.mark.parametrize('lexical_count', [1, 220])
def test_related_chart_counts_full_term_and_semantic_matches(monkeypatch, lexical_count):
    params = engine.validate_params({'q': '졸업 꽃값', 'keywords': '졸업 꽃값', 'limit': 1})
    params['required_terms'] = ['졸업', '꽃값']
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: [])
    day = '2026-11-24'
    rows = [dict(id=1, group_id=None, score=.8, published_date=day, partial_match=True)]
    semantic = [dict(id=i, group_id=None, score=score, published_date=day, partial_match=False)
                for i, score in ((301, .86), (302, .84))]
    matching = list(range(1, lexical_count + 1))
    if lexical_count == 1:
        matching.append(301)
    monthly = [{'month': '2026-11', 'count': len(matching), 'top_keywords': []}]
    article = dict(id=matching[-1] if lexical_count == 1 else 1, published_date=day, publisher='동아일보',
                   title='한 해 배움을 마치는 학생들' if lexical_count == 1 else '꽃 가격 상승')
    conn = Mock()
    conn.execute.side_effect = [
        Mock(**{'fetchall.return_value': []}),
        Mock(**{'fetchall.return_value': [{'id': i} for i in range(1, lexical_count + 1)]}),
        Mock(**{'fetchall.return_value': rows}),
        *([Mock(**{'fetchall.return_value': semantic})] if lexical_count == 1 else []),
        Mock(**{'fetchone.return_value': {'monthly': monthly}}),
        Mock(**{'fetchall.return_value': [article]}),
    ]
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    result = engine._search_db(conn, params, '[1]')
    assert result['stats']['monthly'] == monthly and result['stats']['basis'] == 'related'
    assert conn.execute.call_args_list[-2].args[1][0] == matching
    related, = result['related_events']
    assert related['representative_id'] == article['id'] and not result['events']


@pytest.mark.parametrize('has_core', [True, False])
def test_awards_timeline_uses_search_tier_and_spaced_title(monkeypatch, has_core):
    params = engine.validate_params({'keywords': '디어워즈', 'q': '디 어워즈'})

    def event(article_id, title, day, tier):
        article = {'id': article_id, 'title': title, 'publisher': '동아일보', 'published_date': day}
        return {'group_id': article_id, 'grouped': False, 'tier': tier, 'date_from': day, 'date_to': day,
                'matches': [{**article, 'title_core': tier == 'core'}], 'representative': article,
                'publishers': ['동아일보'], 'article_count': 1}

    core = event(1, '디 어워즈 수상자 발표', '2026-11-12', 'core')
    related = event(2, '안은진 10kg 감량', '2026-02-04', 'related')
    other_award = event(3, '스마일게이트, 스토브인디 어워즈 개최', '2026-01-16', 'core')
    result = {'params': params, 'events': [core, related, other_award] if has_core else [],
              'related_events': [] if has_core else [related]}
    search = Mock(return_value=result)
    keyword_lookup = Mock(return_value=[related])
    monkeypatch.setattr(app, 'search', search)
    monkeypatch.setattr(app, 'timeline_articles', lambda *_: ({}, {}))
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'keyword_timeline_candidates', keyword_lookup)
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda _: {(False, 2): [1, 0]})
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1,0]')
    monkeypatch.setattr(engine, 'timeline_centers', lambda *_: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})

    def chat(system, data, *_args, **_kwargs):
        if isinstance(data, str):
            assert '디어워즈 = 디 어워즈' in system
            if has_core:
                assert core['representative']['title'] in data and related['representative']['title'] not in data
                assert other_award['representative']['title'] not in data
                return {'1': 2}, 1
            assert related['representative']['title'] in data
            return {'1': 1}, 1
        return {}, 1

    ollama = Mock(side_effect=chat)
    monkeypatch.setattr(engine, 'ollama_chat', ollama)
    data, status = app.handle_timeline(urlencode({'kw': '디어워즈', 'q': '디 어워즈'}), as_json=True)
    assert status == 200 and [item['group_id'] for item in data['items']] == [1 if has_core else 2]
    assert not data['filter_relaxed']
    assert ollama.call_count == data['llm_calls'] == 2
    search.assert_called_once_with({'keywords': '디어워즈', 'q': '디 어워즈'}, timeline=True, cover_months=True)
    keyword_lookup.assert_not_called()


def monthly_candidate_row(articles, ids, group_id=None):
    representative = articles[ids[0]]
    return {'month': representative['published_date'][:7], 'group_id': group_id or ids[0],
            'grouped': group_id is not None, 'representative_id': ids[0], 'article_ids': ids,
            'date_from': min(articles[i]['published_date'] for i in ids),
            'date_to': max(articles[i]['published_date'] for i in ids),
            'publishers': sorted({articles[i]['publisher'] for i in ids}), 'article_count': len(ids)}


def test_keyword_search_and_timeline_cover_all_months(monkeypatch):
    amounts = (3, 3, 5, 3, 3, 3, 4, 4, 8, 7, 25)
    chart_counts = (13, 9, 46, 13, 8, 22, 25, 34, 133, 121, 54)
    articles = {
        month * 100 + day: dict(id=month * 100 + day, title=f'김도영 경기 {month}-{day}', publisher='동아일보',
                                published_date=f'2026-{month:02}-{day:02}')
        for month, count in enumerate(amounts, 1) for day in range(1, count + 1)
    }
    articles[103]['published_date'] = '2026-01-31'
    articles[202]['published_date'] = articles[203]['published_date'] = '2026-02-01'
    articles[999] = dict(id=999, title='구단 경기 결과', publisher='동아일보', published_date='2026-11-29')
    articles[950] = {**articles[908], 'id': 950}

    def scoped_ids(_conn, params):
        return [i for i, article in articles.items()
                if i not in params.get('_duplicate_article_ids', ())
                and (params['date_from'] or '') <= article['published_date'] <= (params['date_to'] or '9999')]

    def execute(sql, values=()):
        if sql == 'SHOW transaction_read_only':
            return Mock(**{'fetchone.return_value': {'transaction_read_only': 'on'}})
        if 'SELECT id FROM ranked WHERE position > 1' in sql:
            return Mock(**{'fetchall.return_value': [{'id': 950}]})
        if 'FROM candidates c JOIN articles' in sql:
            rows = [dict(id=i, group_id=None, score=None, published_date=articles[i]['published_date'],
                         publisher=articles[i]['publisher']) for i in values[0]]
            return Mock(**{'fetchall.return_value': rows})
        if 'WHERE a.id=ANY' in sql:
            return Mock(**{'fetchall.return_value': [dict(articles[i], categories=[]) for i in values[0]]})
        return Mock()

    monthly = [{'month': f'2026-{month:02}', 'count': count, 'top_keywords': []}
               for month, count in enumerate(chart_counts, 1)]
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.side_effect = execute
    monkeypatch.setattr(engine.psycopg, 'connect', lambda **_: nullcontext(conn))
    monkeypatch.setattr(engine, 'connection_options', lambda _: {})
    monkeypatch.setattr(engine, 'missing_article_coverage', lambda *_: None)
    monkeypatch.setattr(engine, 'keyword_candidate_ids', scoped_ids)
    monkeypatch.setattr(engine, 'find_core_articles', lambda _, params, *_args: {
        i: True for i in scoped_ids(conn, params) if '김도영' in articles[i]['title']
    })
    monkeypatch.setattr(engine, 'collect_core_stats', lambda *_, cover_months=False: {
        'basis': '핵심', 'monthly': monthly,
        **({'monthly_event_candidates': [monthly_candidate_row(articles, [month * 100 + 1])
                                         for month in range(1, 12)]} if cover_months else {}),
    })
    monkeypatch.setattr(engine, 'event_articles', lambda *_: {})
    monkeypatch.setattr(engine, 'person_result_branches', lambda *_: [])
    boundary = ((False, 103), (False, 201))
    monkeypatch.setattr(engine, 'timeline_centers', lambda *_: {key: [1.0] for key in boundary})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {key: {'경기'} for key in boundary})
    monkeypatch.setattr(engine, 'summarize_timeline', lambda *_args, **_kwargs: ({}, 0))
    monkeypatch.setattr(app, 'timeline_articles', lambda *_: ({}, {}))
    monkeypatch.setattr(app, 'ollama_available', lambda: True)
    monkeypatch.setattr(app, 'list_publishers', lambda: [])

    query = {'ask': '김도영'}
    result, status = app.handle_query(urlencode(query), as_json=True)
    assert status == 200 and result['params']['mode'] == 'keyword'
    assert result['keyword_match'] == 'article_keywords_exact'
    assert result['stats']['monthly'] == monthly
    events = result['events']
    counts = Counter(event['date_from'][:7] for event in events)
    assert len(events) == 20 and sorted(counts) == [row['month'] for row in monthly]
    assert counts['2026-09'] > counts['2026-11'] and counts['2026-10'] > counts['2026-11']
    assert all(event['tier'] == 'core' and event['score'] is None and '김도영' in event['representative']['title']
               for event in events)
    assert all(event['publisher_count'] == 1 and event['importance'] == 1.5 for event in events)
    assert [event['latest_match'] for event in events] == sorted((event['latest_match'] for event in events), reverse=True)
    assert result['related_events'] == []

    timeline, status = app.handle_timeline(urlencode(query), as_json=True)
    assert status == 200 and timeline['header'] == '김도영 · 2026-01 ~ 2026-11'
    assert [month['month'] for month in timeline['months']] == sorted(counts)
    assert all(item['tier'] == 'core' and item['publisher_count'] == 1 and item['importance'] == 1.5
               for item in timeline['items'])

    for options in ({}, {'sort': 'latest'}):
        html, status = app.handle_query(urlencode({'kw': '김도영', **options}), page_path='/classic')
        assert status == 200
        timeline_url = re.search(r'id="timeline-slot" data-url="([^"]+)"', html)[1]
        assert ('sort=latest' in timeline_url) == bool(options)

    for sort in ('latest', 'similarity'):
        explicit, status = app.handle_query(urlencode({**query, 'sort': sort}), as_json=True)
        assert status == 200
        assert [event['group_id'] for event in explicit['events']] == list(range(1125, 1105, -1))
    scoped, status = app.handle_query(urlencode({**query, 'date_from': '2026-09-01', 'date_to': '2026-09-30'}),
                                     as_json=True, timeline=True)
    assert status == 200 and 'monthly_event_selection' not in scoped
    assert [event['group_id'] for event in scoped['events']] == list(range(908, 900, -1))


@pytest.mark.parametrize('has_core, accept', [(True, True), (False, False)])
def test_json_timeline_keeps_chart_months_beyond_search_and_ai_limits(monkeypatch, has_core, accept):
    months = [f'{year}-{month:02}' for year in (2025, 2026) for month in range(1, 13)]
    articles = {i: dict(id=i, title=f'삼성전자 주가 {month} 동향', publisher='동아일보',
                        published_date=f'{month}-05', categories=[])
                for i, month in enumerate(months, 1) if month != '2025-06'}
    chart_ids = list(articles)
    chart = [{'month': month, 'count': int(month != '2025-06'), 'top_keywords': []} for month in months]

    def execute(sql, values):
        if 'AS monthly_event_candidates' in sql:
            assert values[0] == chart_ids
            assert values[-2:] == (chart_ids if has_core else [], engine.TIMELINE_CANDIDATES + engine.TIMELINE_MONTH_LIMIT)
            assert 'PARTITION BY month ORDER BY context_hit DESC, (publisher_count > 1) DESC' in sql
            assert 'title_hit DESC, publisher_count DESC, article_count DESC' in sql
            return Mock(**{'fetchone.return_value': {'monthly': chart, 'monthly_event_candidates': [
                monthly_candidate_row(articles, [i]) for i in chart_ids]}})
        if 'FROM candidates c JOIN articles' in sql:
            rows = [dict(id=i, group_id=None, score=.98, published_date=articles[i]['published_date'], publisher='동아일보')
                    for i in chart_ids[2:]]
            return Mock(**{'fetchall.return_value': rows})
        assert 'WHERE a.id=ANY' in sql
        return Mock(**{'fetchall.return_value': [articles[i].copy() for i in values[0]]})

    conn = Mock()
    conn.execute.side_effect = execute

    def search(params, *, timeline, cover_months):
        params = engine.validate_params(params)
        return {'params': params, **engine._search_db(conn, params, '[1]', timeline=timeline, cover_months=cover_months)}

    judge = Mock(side_effect=lambda events, *_args, **_kwargs: (
        [dict(item, relevance=2) for item in events] if accept else [], 0))
    summarize = Mock(return_value=({}, 0))
    monkeypatch.setattr(app, 'search', search)
    monkeypatch.setattr(app, 'timeline_articles', lambda *_: (
        {(False, i): [article] for i, article in articles.items()}, {}))
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'find_core_articles', lambda *_: dict.fromkeys(chart_ids, True) if has_core else {})
    monkeypatch.setattr(engine, 'keyword_candidate_ids', lambda *_: chart_ids)
    monkeypatch.setattr(engine, 'compound_related_terms', lambda *_: {})
    monkeypatch.setattr(engine, 'related_article_ids', lambda *_: chart_ids)
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1]')
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda events: {
        (event['grouped'], event['group_id']): [1] for event in events})
    monkeypatch.setattr(engine, 'timeline_centers', lambda *_: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    monkeypatch.setattr(engine, 'judge_timeline_relevance', judge)
    monkeypatch.setattr(engine, 'summarize_timeline', summarize)

    data, status = app.handle_timeline(urlencode({'kw': '삼성전자', 'q': '삼성전자 주가'}), as_json=True)
    assert status == 200 and not data['notice']
    assert [month['month'] for month in data['months']] == [row['month'] for row in chart if row['count']]
    assert [item['representative']['id'] for item in data['items']] == chart_ids
    assert all(month['text'] and month['event_count'] == month['article_count'] == 1 for month in data['months'])
    assert all(item['tier'] == ('core' if has_core else 'related') for item in data['items'])
    assert data['items'][0].get('relevance') is None
    assert judge.call_count == 1 and len(judge.call_args.args[0]) == engine.TIMELINE_CANDIDATES
    assert summarize.call_count == int(accept) and data['llm_calls'] == 1 + int(accept)
    if accept:
        assert len(summarize.call_args.args[0]) == engine.TIMELINE_LIMIT
        assert sum(item.get('relevance') == 2 for item in data['items']) == engine.TIMELINE_CANDIDATES


def test_json_timeline_adds_monthly_reports_without_changing_summary(monkeypatch):
    params = engine.validate_params({'keywords': '삼성전자', 'q': '삼성전자 주가'})
    articles = {
        i: dict(id=i, title=f'삼성전자 주가 {label}', published_date=day, publisher=publisher)
        for i, label, day, publisher in [
            (1, '상승', '2026-08-01', '동아일보'), (2, '반등', '2026-08-02', '경향신문'),
            (3, '급락', '2026-08-03', '한겨레'), (4, '급락', '2026-08-03', '동아일보'),
            (5, '신고가', '2026-08-06', '경향신문'), (6, '신고가', '2026-08-06', '한겨레'),
            (7, '보합', '2026-08-31', '동아일보'), (8, '상승', '2026-09-01', '동아일보'),
        ]
    }

    def event(ids, group_id=None):
        row = monthly_candidate_row(articles, ids, group_id)
        return {**row, 'representative': articles[ids[0]], 'matches': [articles[ids[0]]],
                'tier': 'core', '_monthly_article_ids': ids}

    approved, rejected = event([1]), event([2])
    candidates = {'2026-08': [event([3, 4], 101), event([5, 6], 102), rejected, approved, event([7])],
                  '2026-09': [event([8])]}
    result = {'params': params, 'events': [approved, rejected], 'timeline_month_candidates': candidates}
    judge = Mock(return_value=([{
        **engine.merge_timeline_events([approved], {}, {})[0], 'relevance': 2,
    }], 0))
    summarize = Mock(return_value=({}, 0))
    monkeypatch.setattr(engine, '_timeline_cache', OrderedDict())
    monkeypatch.setattr(engine, 'query_embedding', lambda _: '[1]')
    monkeypatch.setattr(engine, 'timeline_representative_vectors', lambda events: {
        (event['grouped'], event['group_id']): [1] for event in events})
    monkeypatch.setattr(engine, 'timeline_centers', lambda *_: {})
    monkeypatch.setattr(engine, 'timeline_keywords', lambda *_: {})
    monkeypatch.setattr(engine, 'judge_timeline_relevance', judge)
    monkeypatch.setattr(engine, 'summarize_timeline', summarize)

    timeline = engine.generate_timeline(result)
    data = app.timeline_json(timeline, params, {(False, i): [article] for i, article in articles.items()}, {})
    assert [item['group_id'] for item in data['items']] == [1, 101, 102, 8]
    assert [[article['id'] for article in item['articles']] for item in data['items']] == [[1], [3, 4], [5, 6], [8]]
    assert [(month['event_count'], month['article_count']) for month in data['months']] == [(3, 5), (1, 1)]
    assert (data['months'][0]['text'], data['months'][0]['source']) == ('8/1 상승', '규칙')
    assert judge.call_count == summarize.call_count == 1 and data['llm_calls'] == 2
    assert [event['title'] for month in summarize.call_args.args[0] for event in month['events']] == [articles[1]['title']]


def test_monthly_event_selection_uses_counts_and_preserves_display_order():
    events = [dict(date_from=f'2026-{month_day}', latest_match=f'2026-{month_day}',
                   article_count=articles, matched_count=matched)
              for month_day, articles, matched in (('10-31', 1, 1), ('10-30', 4, 2), ('10-20', 4, 3),
                                                   ('10-10', 4, 3), ('10-02', 6, 1), ('02-01', 1, 1), ('01-01', 1, 1))]
    monthly = [{'month': month, 'count': count} for month, count in
               (('2026-01', 1), ('2026-02', 100), ('2026-10', 20), ('2026-11', 0))]
    # 2월은 후보가 한 건뿐이므로 남은 자리는 10월에 배분된다.
    assert engine.select_monthly_result_events(events, monthly, 4) == [events[i] for i in (2, 4, 5, 6)]
    assert engine.select_monthly_result_events(events, monthly, 2) == [events[i] for i in (4, 5)]
    events[1]['publishers'] = ['동아일보', '한국일보']
    events[4]['publishers'] = ['동아일보', '동아일보']
    selected = engine.select_monthly_result_events(events, monthly, 4)
    assert selected == [events[i] for i in (1, 4, 5, 6)]
    assert engine.select_monthly_result_events(events, monthly, 2) == [events[i] for i in (1, 5)]


def test_api_latest_keeps_core_first_and_exposes_missing_period(monkeypatch):
    related = {'tier': 'related', 'latest_match': '2026-11-23'}
    core = {'tier': 'core', 'latest_match': '2026-03-15'}
    monkeypatch.setattr(app, 'search', lambda *_, **__: {'events': [related, core]})
    result, status = app.handle_query(urlencode({'q': '김도영', 'sort': 'latest'}), as_json=True)
    assert status == 200 and result['events'] == [core, related]

    params = engine.validate_params({'keywords': '크리스마스', 'date_to': '2025-12-31'})
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {'date_from': date(2026, 1, 1), 'date_to': TODAY}
    notice = engine.article_coverage_notice(conn, params)
    monkeypatch.setattr(app, 'search', lambda *_: {'params': params, **engine.empty_search_result('기간'),
                                                   'data_absence': 'scope_empty', **engine._json_values(notice)})
    result, status = app.handle_query(urlencode({'ask': '작년 크리스마스'}), as_json=True)
    assert status == 200 and result['data_coverage']['date_from'] == '2026-01-01'
    assert '데이터 보유 기간은 2026-01-01' in result['data_absence_message']


def test_empty_timeline_omits_relaxation_notice():
    params = engine.validate_params({'q': '솔로를 위한 크리스마스'})
    data = app.timeline_json({'items': [], 'filter_relaxed': True}, params, {}, {})
    assert data['notice'] and not data['filter_relaxed']


@pytest.mark.parametrize(('question', 'required', 'supporting'), [
    ('솔로를 위한 크리스마스', ['크리스마스'], ['솔로']),
    ('12월 일본 여행', ['일본', '여행'], ['12월']),
    ('올해 최적의 겨울 여행 코스', ['여행'], ['겨울', '코스']),
    ('서울에서 열리는 마라톤 대회', ['마라톤'], ['서울', '대회']),
])
def test_question_splits_required_and_supporting_in_one_call(monkeypatch, question, required, supporting):
    raw = {'keywords': required, 'q': question, 'required_terms': required, 'supporting_terms': supporting}
    chat = Mock(return_value=(raw, 1))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    postprocess = engine.postprocess_question
    monkeypatch.setattr(engine, 'postprocess_question', lambda payload: postprocess(
        payload, lambda words: dict.fromkeys(words, 1)))
    conversion = engine.convert_question(question, TODAY)
    chat.assert_called_once()
    assert conversion['required_terms'] == required
    assert conversion['supporting_terms'] == supporting
    assert conversion['term_source'] == 'model'
    assert set(engine.core_terms(conversion)) == set(required)
    assert set(engine.timeline_aspect_terms(conversion)).isdisjoint(supporting)


@pytest.mark.parametrize('bad_required', [[], ['선물'], ['솔로', '크리스마스'], '크리스마스'])
def test_invalid_partition_preserves_legacy_matching(bad_required):
    question = '솔로를 위한 크리스마스'
    params = {'keywords': ['솔로'], 'q': question}
    partition = engine.question_term_partition({'required_terms': bad_required, 'supporting_terms': ['솔로']},
                                               params, question)
    assert partition['term_source'] == 'legacy'
    assert set(partition['required_terms']) == {'솔로', '크리스마스'}
    assert partition['supporting_terms'] == []


@pytest.mark.parametrize(('question', 'keywords', 'raw', 'required', 'supporting'), [
    ('12월 일본 여행', ['일본', '여행'], {'required_terms': ['12월'], 'supporting_terms': ['일본']},
     ['일본', '여행'], ['12월']),
    ('겨울 여행 코스', ['겨울', '여행'],
     {'required_terms': ['코스'], 'supporting_terms': ['겨울', '여행'], 'subject_names': ['겨울 여행 코스']},
     ['여행'], ['겨울', '코스']),
    ('서울에서 열리는 마라톤 대회', ['마라톤'], {'required_terms': ['마라톤'], 'supporting_terms': ['서울']},
     ['마라톤'], ['서울', '대회']),
])
def test_context_rules_recover_missing_or_swapped_terms(question, keywords, raw, required, supporting):
    partition = engine.question_term_partition(raw, {'keywords': keywords, 'q': question}, question)
    assert partition['required_terms'] == required
    assert partition['supporting_terms'] == supporting


@pytest.mark.parametrize(('question', 'subject', 'aspect'), [
    ('김도영 재판', '김도영', '재판'),
    ('삼성전자 주가', '삼성전자', '주가'),
    ('SK하이닉스 실적', 'SK하이닉스', '실적'),
    ('현대차 실적', '현대차', '실적'),
    ('도쿄 여행', '도쿄', '여행'),
])
def test_subjects_and_registered_aspects_cannot_be_demoted(question, subject, aspect):
    params = {'keywords': [subject], 'q': question}
    raw = {'required_terms': [aspect], 'supporting_terms': [subject], 'subject_names': [subject]}
    partition = engine.question_term_partition(raw, params, question)
    terms = engine.core_terms({**params, **partition})
    assert set(terms) == {subject, aspect}
    assert partition['supporting_terms'] == []
    assert terms[subject][1] == (subject == '김도영')


def test_split_controls_query_evidence_and_core_ranking():
    params = engine.validate_params({'keywords': '솔로 크리스마스', 'q': '솔로를 위한 크리스마스'})
    params.update(required_terms=['크리스마스'], supporting_terms=['솔로'])
    terms = engine.core_terms(params)
    sql, values = engine.core_article_query(params, '[1]', terms)
    assert sql.count('%s') == len(values) and 'article_embeddings' not in sql
    assert not any('솔로' in str(value) for value in values)
    sql, values = engine.candidate_query(params, '[1]', [1, 2, 3], core={1: True, 2: False})
    assert sql.count('%s') == len(values) and 'core_match DESC, support_matches DESC' in sql
    supporting = params['supporting_terms']
    assert engine.article_match_reason({'title': '솔로의 크리스마스'}, terms, supporting) == {
        'title': ['크리스마스', '솔로'], 'keywords': [], 'basis': ''}
    assert engine.article_match_reason({'title': '겨울철 행복', 'evidence_keywords': ['크리스마스']}, terms, supporting) == {
        'title': [], 'keywords': ['크리스마스'], 'basis': ''}
    assert engine.article_match_reason({'title': '영국 싱글차트의 크리스마스 캐럴'}, terms, supporting)['title'] == ['크리스마스']
    person_terms = engine.core_terms({'keywords': ['김도영'], 'q': ''})
    assert engine.article_match_reason({'title': '야구 소식', 'evidence_keywords': ['김도영']},
                                       person_terms, [], basis='semantic')['basis'] == 'semantic'
    assert engine.article_match_reason({'title': '김 도영 복귀'}, person_terms, [])['title'] == ['김도영']
    rows = [dict(id=1, group_id=1, published_date='2026-11-01', tier='core', title_core=True, score=.94),
            dict(id=2, group_id=1, published_date='2026-11-02', tier='core', title_core=False, score=.9,
                 support_matches=1)]
    assert engine.fold_events(rows, 'combined')[0]['matches'][0]['id'] == 2


def test_resolved_timeline_reuses_partition_without_converting(monkeypatch):
    partition = {'required_terms': ['크리스마스'], 'supporting_terms': ['솔로'],
                 'person_names': [], 'term_source': 'model'}
    search = Mock(return_value={'params': {'keywords': ['크리스마스']}, 'term_partition': partition})
    monkeypatch.setattr(app, 'search', search)
    query = urlencode({'ask': '솔로를 위한 크리스마스', 'kw': '크리스마스',
                       'q': '솔로를 위한 크리스마스', 'resolved': '1', 'term_partition': json.dumps(partition)})
    result, status = app.handle_query(query, as_json=True, timeline=True)
    assert status == 200
    assert engine.timeline_params(result)['supporting_terms'] == ['솔로']
    search.assert_called_once_with({'keywords': '크리스마스', 'q': '솔로를 위한 크리스마스'},
                                   timeline=True, term_partition=partition)


def test_timeline_judges_required_topic_without_demanding_support(monkeypatch):
    params = {'keywords': ['솔로', '크리스마스'], 'q': '솔로를 위한 크리스마스',
              'required_terms': ['크리스마스'], 'supporting_terms': ['솔로']}
    event = {'representative': {'title': '새달 앞으로 다가온 크리스마스'}, 'date_from': '2026-11-23',
             'date_to': '2026-11-23'}
    chat = Mock(return_value=({'1': 2}, 1))
    monkeypatch.setattr(engine, 'ollama_chat', chat)
    approved, _ = engine.judge_timeline_relevance([event], params)
    assert approved[0]['relevance'] == 2
    assert '질문: 크리스마스\n' in chat.call_args.args[1]
    assert '솔로' not in chat.call_args.args[1]


def test_person_context_does_not_replace_required_aspect(monkeypatch):
    raw = {'keywords': [], 'q': '손흥민 이적', 'person_names': ['손흥민'], 'person_category': '스포츠',
           'required_terms': ['손흥민'], 'supporting_terms': ['이적']}
    monkeypatch.setattr(engine, 'ollama_chat', lambda *_: (raw, 1))
    monkeypatch.setattr(engine, 'tavily_api_key', lambda: '')
    monkeypatch.setattr(engine, '_ask_cache', OrderedDict())
    conversion = engine.convert_question(raw['q'], TODAY)
    assert conversion['required_terms'] == ['손흥민', '이적']
    assert conversion['supporting_terms'] == []
    assert 'target_category' not in conversion


@pytest.mark.parametrize(('topic', 'expected'), [
    ('연구', ['연구']), ('시즌', ['시즌']), ('졸업 시즌 꽃값', ['졸업', '꽃값']), ('수능 끝나고 할 일', ['수능']),
])
def test_single_generic_word_query_keeps_the_word(topic, expected):
    assert list(engine.core_terms({'q': topic, 'keywords': [], 'person_names': []})) == expected


@pytest.mark.parametrize(('question', 'expected'), [
    ('캉웨이 앨리스', ['캉웨이', '앨리스']),
    ('김도영이 손흥민의', ['김도영', '손흥민']),
    ('뉴진스가 공연', ['뉴진스', '공연']),
    ('마사유키가 앨리스', ['마사유키', '앨리스']),
])
def test_legacy_core_terms_use_particle_boundaries(question, expected):
    assert list(engine.core_terms({'q': question, 'keywords': []})) == expected


@pytest.mark.parametrize('model_name', ['캉웨', '캉웨이'])
def test_question_preserves_full_name_from_source(convert_with_model, model_name):
    result = convert_with_model('캉웨이 앨리스', q=f'{model_name} 앨리스', keywords=[model_name, '앨리스'],
                                required_terms=[model_name, '앨리스'], supporting_terms=[])
    assert result['keywords'] == result['required_terms'] == ['캉웨이', '앨리스']
    terms = engine.core_terms(result)
    assert list(terms) == ['캉웨이', '앨리스']
    assert engine.article_match_reason({'title': '캉웨이와 앨리스'}, terms, [])['title'] == ['캉웨이', '앨리스']


@pytest.mark.parametrize('subject', ['진이', '진이가'])
def test_question_normalizes_particle_on_registered_short_name(convert_with_model, subject):
    result = convert_with_model(f'방탄소년단 {subject} 전역', keywords=[subject], required_terms=[subject],
                                person_names=[subject], supporting_terms=['방탄소년단', '전역'])
    assert result['person_names'] == ['진']
    assert result['required_terms'] == ['진', '전역']
    assert list(engine.core_terms(result)) == ['진', '전역']


@pytest.mark.parametrize('ending', ['하려면', '하는', '했다', '하기', '한다', '하면', '하자', '해야', '된다', '되는', '됐다'])
def test_query_stems_common_verb_endings(ending):
    word = f'공존{ending}'
    params = {'q': f'서울 {word}', 'keywords': []}
    assert '공존' in engine.core_terms(params) and word not in engine.core_terms(params)
    assert engine.ground_query_term('공존', params['q']) == engine.ground_query_term(word, params['q']) == '공존'


@pytest.mark.parametrize('model_topic', ['공존하려면', '공존'])
def test_query_uses_inflected_action_instead_of_location(convert_with_model, model_topic):
    result = convert_with_model('한국경제 서울 공존하려면', q=f'서울 {model_topic}', keywords=['서울'],
                                required_terms=['서울'], supporting_terms=[model_topic])
    assert result['keywords'] == result['required_terms'] == ['공존']
    assert result['supporting_terms'] == ['서울']
    assert list(engine.core_terms(result)) == ['공존']


def test_query_uses_coexistence_as_topic_when_model_requires_seoul(convert_with_model):
    result = convert_with_model('서울 공존', keywords=['서울', '공존'], required_terms=['서울', '공존'],
                                supporting_terms=[])
    assert result['required_terms'] == ['공존']
    assert result['supporting_terms'] == ['서울']
    assert list(engine.core_terms(result)) == ['공존']


@pytest.mark.parametrize(('question', 'place', 'actions'), [
    ('광명시 유치', '광명시', ['유치']),
    ('가평군 행사 개최', '가평군', ['행사', '개최']),
    ('중구에서 지원 사업 추진', '중구', ['지원', '사업', '추진']),
    ('경기도의 사업', '경기도', ['사업']),
    ('조치원읍 개최', '조치원읍', ['개최']),
    ('별량면 행사', '별량면', ['행사']),
    ('광명 유치', '광명', ['유치']),
    ('성남 사업', '성남', ['사업']),
    ('제주에서 행사를 개최한다', '제주', ['행사', '개최']),
])
def test_generic_local_event_keeps_place_and_actions(convert_with_model, question, place, actions):
    result = convert_with_model(question, q=actions[-1], keywords=[actions[-1]],
                                required_terms=[actions[-1]], supporting_terms=[place])
    assert result['required_terms'] == [place, *actions]
    assert result['supporting_terms'] == []
    assert list(engine.core_terms(result)) == [place, *actions]


def test_gwangmyeong_bid_requires_both_title_terms(convert_with_model):
    result = convert_with_model('광명시 유치', keywords=['유치'], required_terms=['유치'], supporting_terms=[])
    terms = engine.core_terms(result)
    title = '광명시, 5만석 규모 돔구장 유치 서명…'
    other_title = '다른 지역 돔구장 유치 서명'
    assert engine.article_match_reason({'title': title}, terms, [])['title'] == ['광명시', '유치']
    assert engine.article_match_reason({'title': other_title}, terms, [])['title'] == ['유치']


@pytest.mark.parametrize(('question', 'context', 'required'), [
    ('서울 공존', '서울', ['공존']),
    ('광명시 돔구장 유치', '광명시', ['돔구장', '유치']),
    ('인구 지원', '인구', ['지원']),
    ('서울 야구 대회', '서울', ['야구', '대회']),
])
def test_local_event_rule_leaves_specific_topics_and_non_places_alone(convert_with_model, question, context, required):
    result = convert_with_model(question, keywords=required, required_terms=required, supporting_terms=[context])
    assert list(engine.core_terms(result)) == required
    assert result['supporting_terms'] == [context]
