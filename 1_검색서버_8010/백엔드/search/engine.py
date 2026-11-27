"""필수 키워드 AND 안에서 자연어 주제를 검색한다. DB는 읽기 전용."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import json
import logging
import math
import os
from pathlib import Path
import re
import string
import sys
import unicodedata
from threading import Event, Lock, Thread
from time import perf_counter, monotonic
from urllib.request import Request, urlopen
from urllib.error import URLError

from dotenv import dotenv_values

if __package__:
    from . import prompts
else:
    import prompts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import psycopg
from psycopg.rows import dict_row
from load_bigkinds import DEFAULT_ENV, connection_options
from embed_articles import DEFAULT_CACHE, encode_titles, load_model

logger = logging.getLogger(__name__)

RESULT_MIN_COS = 0.85
VECTOR_CANDIDATES = 200
TITLE_MATCH_COS_MARGIN = 0.07
ENTITY_ALIASES = {
    '기아타이거즈': ('기아타이거즈', '기아 타이거즈', 'KIA', '타이거즈'),
    '삼성라이온즈': ('삼성라이온즈', '삼성 라이온즈', '라이온즈'),
    '삼성전자': ('삼성전자', '삼성 전자'),
    'SK하이닉스': ('SK하이닉스', 'SK 하이닉스', 'SK하닉', '하이닉스'),
    '오징어게임': ('오징어게임', '오징어 게임'),
}
for short, standard in prompts.QUERY_ALIASES.items():
    ENTITY_ALIASES[standard] = (*ENTITY_ALIASES.get(standard, (standard,)), short)
TEAM_NAMES = {'기아타이거즈', '삼성라이온즈'}
REGISTERED_PERSON_NAMES = {'김도영'}
TITLE_REQUIRED_PEOPLE = REGISTERED_PERSON_NAMES | {
    '손흥민', '한강', '이재명', '안세영', '윤석열', '강선우', '이승엽', '김민재', '이강인', '황희찬',
}
GROUP_MEMBERS = {
    ('블랙핑크', 'BLACKPINK'): ('지수', '제니', '로제', '리사'),
    ('방탄소년단', 'BTS'): ('RM', '진', '슈가', '제이홉', '지민', '뷔', '정국'),
    ('에스파', 'aespa'): ('카리나', '윈터', '지젤', '닝닝'),
}
JOINT_QUERY_SUBJECTS = {'하이브', '뉴진스'}
NON_PERSON_ENTITIES = ENTITY_ALIASES.keys() | JOINT_QUERY_SUBJECTS | {'현대차'} | {
    name.casefold() for aliases in GROUP_MEMBERS for name in aliases
}
PERSON_BRANCH_CONTEXTS = ('야구선수', '기업인')
PERSON_BRANCH_MIN_EVENTS = 2
MAJOR_CATEGORIES = ('경제', '정치', '사회', '지역', '국제', '문화', 'IT·과학', '스포츠',
                    '미분류', '생활')
MAJOR_CATEGORY_ALIASES = {'IT_과학': 'IT·과학', '세계': '국제', '생활·문화': '문화'}
BUSINESS_CONTEXT_WORDS = {'기업인', '경영인', 'ceo'}
POLITICAL_CONTEXT_WORDS = {'정치인', '의원', '국회의원'}
PERSON_CONTEXT_WORDS = BUSINESS_CONTEXT_WORDS | POLITICAL_CONTEXT_WORDS | {
    '선수', '야구선수', '가수', '배우', '연예인', '교수', '학자',
}
PERSON_TITLE_WORDS = {
    '의원', '국회의원', '시의원', '도의원', '구의원', '군의원', '회장', '부회장', '사장', '대표',
    '대표이사', '의장', '감독', '코치', '장관', '차관', '대통령', '총리', '시장', '도지사', '선수',
    '작가', '기자', '변호사',
}
QUERY_DOMAIN_WORDS = {'유통'}
QUERY_LOCATION_WORDS = {'서울', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '제주',
                        '강원', '충북', '충남', '전북', '전남', '경북', '경남'}
QUERY_TIME_WORDS = {'시즌', '철', '기간'}
SEASON_CONTEXT_WORDS = {'봄', '여름', '가을', '겨울', '봄철', '여름철', '가을철', '겨울철', '연말', '연초', '이사철'}
PERSON_DESCRIPTORS = {'솔로'}
CONTEXT_EXCLUSIONS = {
    **{word: ('스포츠',) for word in PERSON_CONTEXT_WORDS - {'선수', '야구선수'}},
    **{word: ('경제',) for word in ('선수', '야구선수', '야구', '축구', '농구', '배구', '순위', '구단')},
}
CONTEXT_SUFFIX = r'(?:\s+(?:관련\s+)?(?:기사|뉴스|검색))?[?？.!\s]*'
PERSON_ROLE_QUERY = re.compile(r'([가-힣A-Za-z]+)\s+([가-힣A-Za-z]+)' + CONTEXT_SUFFIX)
CATEGORY_RANK_PENALTY = 0.12
NORMALIZED_MAJOR_SQL = (
    "CASE btrim(c.major) "
    + ' '.join(f"WHEN '{legacy}' THEN '{display}'" for legacy, display in MAJOR_CATEGORY_ALIASES.items())
    + " ELSE NULLIF(btrim(c.major), '') END"
)
BASEBALL_CATEGORY_SQL = f'''EXISTS (
    SELECT 1 FROM article_categories c WHERE c.article_id=a.id
    AND ({NORMALIZED_MAJOR_SQL})='스포츠' AND btrim(c.minor) LIKE '야구%%'
)'''
CONTEXT_PATTERNS = {
    '순위': r'(?<![0-9])순위|[0-9]+\s*위(?![0-9])|선두|매직\s*넘버',
    '기업인': r'사장|(?<!국가)대표(?!팀)|CEO|회장|상무|임원|경영인',
}
EARNINGS_CUES = r'영업\s*(?:이익|익|손실)|매출|순\s*이익|실적|어닝|흑자|적자|수익성|이익률'
EARNINGS_ASPECT_CUES = rf'{EARNINGS_CUES}|분기'
EARNINGS_ADJACENT = (r'성과\s*급|연봉|급여|월급|보너스|'
                     r'수익률.{0,20}(?:인증|자랑)|(?:개인\s*투자자|개미|인증|자랑).{0,20}수익률')
TOPIC_ASPECTS = {
    '주가': (r'주가(?!\s*조작)|주식(?!\s*회사)|증시|코스피|코스닥|시총|시가\s*총액|'
           r'52\s*주(?!년)|신고가|신저가|급등|급락|외국인.{0,8}순\s*매[수도]|목표가'),
    '실적': rf'^(?!.*(?:{EARNINGS_ADJACENT})).*(?:{EARNINGS_ASPECT_CUES})',
    '재판': r'재판|법정|법원|공판|심리|선고|판결|항소|상고',
    '규제': r'규제|제재|과징금|감독\s*기관|시행령|금지|인허가',
    '꽃값': r'꽃\s*(?:값|가격)',
}
QUERY_EVENT_ASPECTS = (
    '이적', '전당대회', '지지율', '불화설', '순위', '개인정보', '유출', '피해', '하락', '급락',
    '협상', '일정', '외압', '구조조정', '이혼', '부상', '투약', '경질', '열애',
)
PERSON_EVENT_ASPECTS = ('전역',)
POLITICAL_ROLES = r'(?:국회|[시도군구])?의원|장관|도지사|(?!(?:해외|국내|주식|금융)시장)[가-힣]{2,}시장'
POLITICAL_PARTIES = r'[가-힣]{2,}당|국민의힘'
PERSON_TITLE_PATTERNS = {
    '기업인': CONTEXT_PATTERNS['기업인'] + r'|공동\s*의장|투자\s*이끈|[0-9]+\s*(?:억|조)\s*(?:원\s*)?맡긴',
    # 후보·시장·당처럼 뜻이 겹치는 표현에는 직함의 위치나 선거·정당 단서를 요구한다.
    '정치인': (rf'(?:{POLITICAL_ROLES}|{POLITICAL_PARTIES})\s+{{name}}|'
              rf'{{name}}(?:\s+|\s*[,，]\s*)(?:(?:전|현)\s+)?'
              rf'(?:{POLITICAL_ROLES}|시장|(?:예비\s*)?후보)(?:[은는이가을를의와과도]|으로|에게)?(?![가-힣])|'
              r'{name}.{0,12}(?:입당|탈당|공천|출마|당선)|'
              rf'(?:대선|총선|지방선거|{POLITICAL_PARTIES})\s*(?:예비\s*)?후보\s+{{name}}'),
}
CONTEXT_FOCUS = {
    '순위': r'정규\s*시즌.{0,10}[0-9]+\s*위|[0-9]+\s*위\s*(?:확정|확보)|매직\s*넘버',
    '기업인': r'(?:사장|대표|CEO|회장|상무)(?:으로|에)?\s*(?:후보|지명)|신임\s*(?:사장|대표|CEO|회장|상무)',
}
NATIONAL_TEAM_ALIASES = (*ENTITY_ALIASES['국가대표'], '대표팀')
SPORT_TITLE_PATTERNS = {
    '축구': r'축구|홍명보|손흥민|이강인|김민재|황희찬|클린스만|월드컵|A\s*매치|K\s*리그|FIFA|골키퍼|골문',
    '야구': r'야구|홈런|타율|타점|투수|포수|타자|도루|타이거즈|WBC|KBO|APBC|프리미어\s*12|이닝',
    '농구': r'농구|\m(?:KBL|WKBL|NBA|FIBA)\M|덩크|리바운드|3점슛',
    '배구': r'배구|\m(?:KOVO|FIVB|VNL)\M|V\s*리그|김연경|스파이크|리베로',
    '탁구': r'탁구|신유빈|\m(?:ITTF|WTT)\M',
    '배드민턴': r'배드민턴|안세영|셔틀콕|\mBWF\M',
    '양궁': r'양궁|리커브|컴파운드',
    '수영': r'수영|자유형|접영|황선우',
    '태권도': r'태권도|겨루기|품새',
    '빙상': r'빙상|쇼트트랙|스피드\s*스케이팅|피겨',
    '테니스': r'테니스|윔블던|\m(?:ATP|WTA)\M',
    '골프': r'골프|\m(?:PGA|LPGA|KLPGA)\M|버디|이글',
    '핸드볼': r'핸드볼|\mIHF\M',
    '럭비': r'럭비|럭비월드컵',
}
NONSPORT_TITLE = (r'^\s*[\[【].{0,20}(?:칼럼|사설|기고|시론|오피니언)'
                  r'|(?<![A-Za-z0-9])AI(?![A-Za-z0-9])|인공지능')
AUTO_CONTEXT = r'기아차|현대차|자동차|판매|\mEV\M|전기차|완성차|신차|출고|SUV'
BASEBALL_CONTEXT = (r'야구|홈런|타율|타점|타자|타격|안타|도루|구단|그라운드|KIA|타이거즈|WBC|KBO|APBC|'
                    r'시즌|경기|이닝|대표팀|국가대표|태극마크|선수|기아\s+[가-힣]{2,4}(?=\s|[,，·]|$)')
WEATHER_DIGEST = r'^\s*(?:\[|【)?\s*(?:오늘의\s*날씨|내일의\s*날씨|기상특보|전국\s*날씨)'
ROUTINE_TITLE = (r'^\s*(?:\[|【|\()?\s*(?:(?:오늘|내일)의?\s*(?:주요일정|일정|날씨)|'
                 r'(?:정치|경제|사회|증시|부처)\s*주요\s*일정|주요\s*(?:일정|신문\s*사설)|'
                 r'(?:모닝|아침|뉴스)\s*브리핑|(?:조간|석간)\s*(?:헤드라인|브리핑)|'
                 r'(?:오늘|내일)의\s*(?:인사|부고))')
SHORT_TITLE_SQL = "char_length(regexp_replace(a.title, '[^[:alnum:]]', '', 'g')) <= 9"
TITLE_SIGNALS_SQL = """a.title ~* %s AS name_match, a.title ~* %s AS context_match,
                       a.title ~* %s AS wrong_context, a.title ~* %s AS focus_match"""
TITLE_RANK_SQL = """COALESCE(score, 0.78) + 0.035 * name_match::int
    + 0.20 * (name_match AND context_match)::int + 0.10 * (name_match AND focus_match)::int
    - 0.30 * wrong_context::int"""
HNSW_EF_SEARCH = 200
MAX_LIMIT = 50
MERGE_GAP_DAYS = 3
MERGE_MIN_COS = 0.88
MERGE_MAX_SPAN_DAYS = 14
MERGE_KEYWORD_LIMIT = 10
# 전역 기사 df 5% 이상인 일반 직함·기관·지역어와 관세(비교 15사건 모두 등장).
# df 근거: .verification/search_v0188/keyword_data.json 및 작업기록 v0.1.88.
MERGE_GENERAL_WORDS = frozenset({
    '미국', '대통령', '정부', '한국', '국민', '서울', '대표', '의원', '조사', '혐의', '경찰',
    '관세',
})
TIMELINE_CANDIDATES = 15
KEYWORD_TIMELINE_CANDIDATES = 50
TIMELINE_COVERAGE_CANDIDATES = 12
TIMELINE_RELEVANCE_BATCH = 8
TIMELINE_Q_MIN_COS = 0.83
TIMELINE_LIMIT = 10
TIMELINE_MONTH_LIMIT = 3
PROMPT_REVISION = 'v0.2.20'
QUERY_REVISION = 'v0.2.171'
TIMELINE_REVISION = 'v0.2.160'
FOLLOWUP_WINDOW_DAYS = 60
FOLLOWUP_MIN_COS = RESULT_MIN_COS
FOLLOWUP_DONGA_LIMIT = 10
FOLLOWUP_OTHER_LIMIT = 5
MONTH_UNCERTAINTY = re.compile(r'우려|가능성|검토|전망|관측|예상|추진|예고|[?？]$|(?:나서나|할까|될까)$')
MONTH_DIRECTION = {
    '급상승': '급등', '급등': '급등', '치솟': '급등', '상승': '상승', '올랐': '상승', '올라': '상승',
    '뛰었': '상승', '오른': '상승',
    '반등': '반등', '회복': '회복', '신고가': '신고가', '최고가': '최고가', '강세': '강세', '상한가': '상한가',
    '급하락': '급락', '급락': '급락', '폭락': '급락', '하락': '하락', '내렸': '하락', '내려': '하락',
    '떨어졌': '하락', '떨어진': '하락', '밀렸': '하락',
    '약세': '약세', '하한가': '하한가', '신저가': '신저가', '최저가': '최저가', '보합': '보합',
    '급증': '급증', '증가': '증가', '급감': '급감', '감소': '감소', '확산': '확산',
}
MONTH_DIRECTION_PATTERN = re.compile('|'.join(MONTH_DIRECTION))
MONTH_MARKET = re.compile(r'코스피|코스닥|증시|증권시장')
MONTH_MARKET_TITLE = re.compile(r'^(?:\[[^\]]+\]\s*)*((?:코스피|코스닥)(?:[-·/](?:코스피|코스닥))?|증시|증권시장)(?![가-힣])')
MONTH_PRICE_MOVE = re.compile(r'\d+(?:[.,]\d+)*(?:\s*%대?|\s*(?:억|만|천)?\s*원대?|\s*주)\s*$')
MONTH_EARNINGS = re.compile(r'영업(?:이익|익)\s*\d+(?:[.,]\d+)*\s*(?:조|억)(?:원)?\s*(?:인데도?|에도)')
MONTH_TRADING_HALT = re.compile(r'서킷\s*브레이커|사이드카')
# 동의어별 첫 등장 항목을 공유한다. 정책의 서로 다른 행동은 별도 단계다.
TIMELINE_STAGE_ALIASES = {
    '의혹': ('의혹', '폭로'),
    '수사': ('수사', '압수수색'),
    '조사': ('소환', '출석', '조사'),
    '영장청구': ('구속영장청구', '구속영장신청', '영장청구', '영장신청'),
    '체포동의안': ('체포동의안',),
    '구속': ('구속영장발부', '영장발부', '구속심사', '영장실질심사', '법정구속', '구속'),
    '기소': ('구속기소', '불구속기소', '기소', '송치'),
    '재판': ('공판', '재판'),
    '구형': ('구형',),
    '석방': ('보석', '석방'),
    '선고': ('1심판결', '1심선고', '1심', '선고', '판결'),
    '항소': ('항소심판결', '항소심선고', '2심판결', '2심선고', '항소심', '2심', '항소'),
    '상고': ('상고심판결', '상고심선고', '3심판결', '3심선고', '상고심', '3심', '상고', '확정'),
    '사퇴': ('사퇴',),
    '사과': ('사과',),
    '발표': ('발표',),
    '합의': ('합의',),
    '발효': ('발효',),
    '부과': ('부과',),
    '인하': ('인하',),
    '인상': ('인상',),
}
TIMELINE_STAGE_BY_ALIAS = {alias: stage for stage, aliases in TIMELINE_STAGE_ALIASES.items() for alias in aliases}
# 긴 표현을 먼저 소비해 영장 신청을 구속으로, 항소심 판결을 1심 선고로 중복 인식하지 않는다.
TIMELINE_STAGE_PATTERN = re.compile('|'.join(map(re.escape, sorted(TIMELINE_STAGE_BY_ALIAS, key=len, reverse=True))))
# 월별 주요 키워드에서 제외할 일반어. 검색에 쓴 필수 키워드도 함께 제외한다.
KEYWORD_STOPWORDS = frozenset({
    "이날", "지난", "이번", "관련", "최근", "오전", "오후", "올해", "지난해",
    "이후", "당시", "한편", "이같이", "오늘", "내일", "등",
    "어느", "어떤", "무슨", "이런", "그런", "저런", "모든", "여러", "각",
    "동안", "지금", "이제", "요즘", "때문", "통해", "위해", "대해",
})
_model = None
_model_lock = Lock()
_warmup_lock = Lock()
_warmup_thread = None
_warmup_done = Event()
_warmup_failed = False
# ponytail: Ollama 호출은 프로세스별 전역 잠금으로 직렬화한다. 다중 워커 간 공유하지 않는다.
_llm_lock = Lock()
_web_conversion_lock = Lock()
_cache_lock = Lock()
_ask_cache = OrderedDict()
_category_cache = OrderedDict()
_timeline_cache = OrderedDict()
LLM_TIMEOUT = 60
TAVILY_TIMEOUT = 5
TAVILY_RESULTS = 4
WEB_CONVERSION_TIMEOUT = 8
WEB_KNOWN_MIN_ARTICLES = 3
WEB_TITLE_SAMPLE = 32
WEB_SEARCH_TOOL = {"type": "function", "function": {
    "name": "web_search", "description": "미등록 줄임말·신조어·동명이인·고유명사의 뜻과 명칭 확인",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}}
WEB_LOOKUP_SCHEMA = {
    "type": "object", "properties": {"query": {"type": ["string", "null"]}},
    "required": ["query"], "additionalProperties": False,
}
NEWS_WORDS = frozenset({'뉴스', '소식', '기사'})
ASK_STOPWORDS = KEYWORD_STOPWORDS | NEWS_WORDS | frozenset({
    "요즘", "연구", "결과", "내용", "사건", "발표", "누가", "언제", "어떻게", "해당",
})
QUERY_WORD = re.compile(r'[가-힣]+|[A-Za-z0-9]+')
TOPIC_WORD = re.compile(r'[가-힣A-Za-z0-9]+')
SHORT_LATIN_KEYWORD = re.compile(r'[A-Za-z][A-Za-z0-9]{1,7}')
ALIAS_PARTICLE = r'에서|에게|으로|이랑|은|는|이|가|을|를|의|에|로|도|와|과'
TITLE_PARTICLE = (rf'{ALIAS_PARTICLE}|(?:에서|에게|으로|한테|로|에|와|과)(?:서|도|는|의|만|부터|까지)?|'
                  r'이(?:나|라도|다|냐|며|고|라고|라는)|인가|에겐|만|나|라도|까지|부터|보다|처럼|마저|조차|밖에')
SHORT_NAME_PARTICLE = r'이(?:가|는|의|와|도|를)?|은|는|가|을|를'
SURNAME_HONORIFIC = re.compile(r'[가-힣]씨')
QUERY_PARTICLE_SUFFIX = re.compile(rf'([가-힣A-Za-z0-9]+?)({ALIAS_PARTICLE}|이란|란)')
LOCAL_EVENT_WORD = re.compile(
    rf'(유치|개최|추진|사업|지원|행사|대회|공모|조성|개발|건설|개관|착공|준공)(?:{ALIAS_PARTICLE})?'
)
LOCAL_PLACE_NAMES = QUERY_LOCATION_WORDS | {
    '광명', '성남', '수원', '고양', '용인', '전주', '청주', '춘천', '강릉', '포항', '강원도', '제주도',
}
LOCAL_PLACE_WORD = re.compile(rf'({"|".join(sorted(LOCAL_PLACE_NAMES, key=lambda name: (-len(name), name)))}'
                              rf'|[가-힣]+?[시군구도읍면])(?:{ALIAS_PARTICLE})?')
NON_PLACE_WORDS = {
    '도시', '전시', '인구', '가구', '기구', '도구', '요구', '청구', '복구', '제도', '보도', '지도', '정도', '속도',
    '별도', '국도', '철도', '유도', '축구', '야구', '농구', '배구', '당구', '탁구', '국군', '육군', '해군', '공군', '미군',
}
QUERY_VERB_ENDING = re.compile(r'([가-힣]{2,}?)(?:하(?:려면|는|기|기로|다|면|자|고|지|도록)|'
                              r'한다|해야|했다|했습니다|합니다|되(?:려면|는|기|다|면|고|지|도록)|'
                              r'된다|돼야|됐다|됐습니다|됩니다)')
FOUR_DAY_WORKWEEK = re.compile(rf'(?<!\w)주\s*4일제(?:{ALIAS_PARTICLE})?(?!\w)')
QUERY_ACTION_FILLER = re.compile(rf'(?<!\w)(?:끝나고|할\s*일)(?:{ALIAS_PARTICLE})?(?!\w)')
QUERY_CONTEXT_FILLER = re.compile(rf'(?<!\w)(?:시즌|철|기간|끝나고|할\s*일)(?:{ALIAS_PARTICLE})?(?!\w)')
SAMSUNG_STOCK_ALIAS = re.compile(rf'(?<!\w)삼성(?:의)?(?=\s+주가(?:{ALIAS_PARTICLE})?(?!\w))')
QUERY_ALIAS_PATTERN = re.compile(
    rf'(?<![가-힣A-Za-z0-9])({"|".join(prompts.QUERY_ALIASES)})(?:{ALIAS_PARTICLE})?(?![가-힣A-Za-z0-9])'
)
TOPIC_FILLER = KEYWORD_STOPWORDS | frozenset({
    '기사', '뉴스', '요즘', '좀', '왜', '뭐', '누가', '언제', '어떻게', '이렇게',
    '얼마나', '관련해서', '나온', '나와', '나왔어', '늘었어', '됐어', '알려줘', '찾아줘',
})
TIMELINE_ASPECT_FILLER = TOPIC_FILLER | NEWS_WORDS | {
    '근황', '최신', '보도', '관련된', '대한', '대해', '동향', '추이', '흐름', '주제', '정리', '알려주세요',
}
WEB_FILLER = ASK_STOPWORDS | TOPIC_FILLER | {
    '많아', '많아요', '많다던데', '내린다던데', '받았어', '뭔가요', '뭐야', '뭐예요', '뭐지',
    '무슨', '뜻이야', '뜻인가요', '뜻', '의미', '설명해줘', '알려주세요', '찾아주세요', '하나요',
}
PUBLISHER_WORD = (r'[가-힣]+(?:일보|신문)|한겨레|스포츠\s*(?:동아|조선)|연합뉴스|뉴스1|뉴시스|'
                  r'매일경제|한국경제|KBS|MBC|SBS|JTBC|YTN')
DATE_PARTICLE = r'(?:\s*동안|간|의|에|은|는|을|를)?(?!\w)'
NATIVE_DAY_COUNTS = {'하루': 1, '이틀': 2, '사흘': 3, '나흘': 4, '닷새': 5, '엿새': 6, '이레': 7, '열흘': 10, '보름': 15}
ROLLING_PERIOD = re.compile(
    rf'(?<!\w)(?:최근|지난)\s*(?:(\d{{1,3}})\s*(개월|일|주일|주)|일\s*주일|한\s*달|'
    rf'({"|".join(NATIVE_DAY_COUNTS)})){DATE_PARTICLE}')
CALENDAR_WEEKEND = re.compile(rf'(?<!\w)(이번|지난|다음)\s*주말{DATE_PARTICLE}')
CALENDAR_WEEK = re.compile(rf'(?<!\w)(이번|금|지난|저번)\s*주{DATE_PARTICLE}')
CALENDAR_SUBPERIOD = re.compile(
    rf'(?<!\w)(?:(올해|금년|작년|재작년|지난해|내년|후년|\d{{4}}\s*년)\s*)?'
    rf'(상반기|하반기|[1-4]\s*분기){DATE_PARTICLE}')
HOLIDAY_CONTEXT = re.compile(rf'(?<!\w)(설|설날|추석)\s+연휴(?:{ALIAS_PARTICLE})?(?!\w)')
CALENDAR_DAY = r'(?:(\d{4})\s*년\s*)?(?:(\d{1,2})\s*월\s*)?(\d{1,2})\s*일'
CALENDAR_DAY_PARTICLE = rf'(?:{ALIAS_PARTICLE})?(?!\w)'
CALENDAR_RANGE = re.compile(
    rf'(?<!\d){CALENDAR_DAY}\s*(?:부터|[~～])\s*{CALENDAR_DAY}(?:\s*까지)?{CALENDAR_DAY_PARTICLE}')
DATE_RANGE_SUFFIX = r'부터|까지|이후|이전'
CALENDAR_MONTH_DAY = re.compile(
    rf'(?<!\d)(?:(\d{{4}})\s*년\s*)?(\d{{1,2}})\s*월\s*(\d{{1,2}})\s*일'
    rf'(?:{CALENDAR_DAY_PARTICLE}|(?={DATE_RANGE_SUFFIX}))')
RELATIVE_MONTH_DAY = re.compile(
    rf'(?<!\w)(지난|저번|이번)\s*달\s*(\d{{1,2}})\s*일{CALENDAR_DAY_PARTICLE}')
CALENDAR_RANGE_EDGE = rf'(?<!\w)\d{{1,2}}\s*일(?=\s*(?:{DATE_RANGE_SUFFIX})(?!\w))'
DAYS_AGO = re.compile(rf'(?<!\w)(\d{{1,3}})\s*일\s*전{DATE_PARTICLE}')
RELATIVE_DAY_OFFSETS = {'오늘': 0, '어제': -1, '그저께': -2, '그제': -2, '엊그제': -2, '내일': 1}
RELATIVE_DAY = re.compile(rf'(?<!\w)({"|".join(RELATIVE_DAY_OFFSETS)}){DATE_PARTICLE}')
TOPIC_MONTH = re.compile(r'(?<!\d)(?:1[0-2]|0?[1-9])\s*월(?!요일|\s*\d{1,2}\s*일|\s*(?:부터|까지|이후|이전))')
SEASONAL_INTENT = re.compile(r'여행|휴가|크리스마스|연말|피서|휴양|나들이|여행지|명소|축제|단풍|꽃구경|가볼\s*만한|데이트|코스\s*추천')
QUESTION_DATE_CUE = re.compile(
    rf'{CALENDAR_RANGE.pattern}|{CALENDAR_MONTH_DAY.pattern}|{RELATIVE_MONTH_DAY.pattern}|'
    rf'{CALENDAR_RANGE_EDGE}|{DAYS_AGO.pattern}|{RELATIVE_DAY.pattern}|'
    rf'{ROLLING_PERIOD.pattern}|{CALENDAR_SUBPERIOD.pattern}|{CALENDAR_WEEKEND.pattern}|{CALENDAR_WEEK.pattern}|'
    r'(?:최근|지난)\s*\d+\s*(?:개월|년)|'
    r'(?<!\d)\d{4}(?:[-./]\d{1,2}(?:[-./]\d{1,2})?|\s*년)|'
    r'(?<!\d)\d{1,2}\s*월(?!요일)|'
    r'(?:지난|저번|이번)\s*달|'
    r'지난해|(?:재)?작년|올해|금년|내년|후년'
)
QUESTION_YEAR_CUE = re.compile(
    r'(?<!\d)(?:\d{4}(?!\d)|\d{1,2}\s*년)|(?:재)?작년|올해|금년|내년|후년|전년|(?:지난|저번|이번|다음)\s*해'
)


def title_entity_pattern(aliases, *, ignore_spacing: bool = False) -> str:
    patterns = []
    for alias in aliases:
        compact = ''.join(alias.split())
        if ignore_spacing and re.fullmatch(r'[가-힣A-Za-z0-9]{2,30}', compact):
            literal = r'\s*'.join(map(re.escape, compact))
        else:
            literal = re.escape(alias) if alias.isascii() else r'\s*'.join(map(re.escape, alias.split()))
        if ignore_spacing or (not alias.isascii() and len(alias.split()) > 1) or alias in prompts.QUERY_ALIASES:
            # PostgreSQL 환경에 따라 \w가 한글을 포함하지 않는다.
            literal = rf'(?<![가-힣A-Za-z0-9_]){literal}(?=(?:{TITLE_PARTICLE})?(?![가-힣A-Za-z0-9_]))'
        elif alias.isascii():
            literal = rf'(?<![A-Za-z0-9]){literal}(?![A-Za-z0-9])'
        patterns.append(literal)
    return '|'.join(patterns)


def canonical_keyword(keyword: str) -> str:
    return next((name for name, aliases in ENTITY_ALIASES.items()
                 if keyword.casefold() in {alias.casefold() for alias in aliases}),
                COMPANY_ALIAS_KEYS.get(''.join(keyword.split()).casefold(), keyword))


def is_query_particle(stem: str, suffix: str | None) -> bool:
    compact = ''.join(stem.split())
    if suffix not in ('이', '가') or not re.fullmatch(r'[가-힣]{2,}', compact) or compact in TITLE_REQUIRED_PEOPLE:
        return True
    # 두 음절 어간은 고유명사와 구별하기 어렵다. 더 길어도 받침과 맞는 조사만 허용한다.
    has_final = (ord(compact[-1]) - ord('가')) % 28 != 0
    return len(compact) > 2 and has_final == (suffix == '이')


def query_verb_stem(word: str) -> str:
    match = QUERY_VERB_ENDING.fullmatch(word)
    return match[1] if match else word


def normalize_query_term(text: str, names=()) -> str:
    names = sorted(TITLE_REQUIRED_PEOPLE | set(names), key=lambda name: (-len(name), name))

    def normalize(match):
        word = match[0]
        if word in names:
            return word
        for name in names:
            if word.startswith(name):
                particles = SHORT_NAME_PARTICLE if len(name) == 1 else ALIAS_PARTICLE
                if re.fullmatch(particles, word[len(name):]):
                    return name
        return query_verb_stem(word)

    return TOPIC_WORD.sub(normalize, text)


def national_team_sports(topic: str, keywords=()) -> list[str]:
    topic = ' '.join((topic, *keywords))
    if not re.search(title_entity_pattern(NATIONAL_TEAM_ALIASES), topic, re.IGNORECASE):
        return []
    return [sport for sport in SPORT_TITLE_PATTERNS if re.search(
        rf'(?<!\w){sport}(?:{ALIAS_PARTICLE})?(?!\w)', topic)]


def lexical_role_terms(topic: str) -> list[str]:
    """직함 약칭이 등록된 관점어와 결합하면 두 단어가 모두 검색 대상이다."""
    if match := PERSON_ROLE_QUERY.fullmatch(topic):
        left, right = match.groups()
        for role, subject in ((left, right), (right, left)):
            if (role.isascii() and role.casefold() in PERSON_CONTEXT_WORDS
                    and (subject in QUERY_EVENT_ASPECTS or any(
                        re.fullmatch(pattern, subject) for pattern in TOPIC_ASPECTS.values()))):
                return [left, right]
    return []


def context_words(topic: str) -> set[str]:
    return set(re.findall(r'[가-힣a-z0-9]+', topic.casefold())) & CONTEXT_EXCLUSIONS.keys()


def topic_aspects(topic: str) -> dict[str, str]:
    return {word: pattern for word, pattern in TOPIC_ASPECTS.items() if re.search(
        rf'(?<!\w){word}(?:{ALIAS_PARTICLE})?(?!\w)' + (r'(?!\s*조작)' if word == '주가' else ''), topic)}


def topic_aspect_pattern(topic: str) -> str:
    return '|'.join(topic_aspects(topic).values())


def excluded_categories(topic: str) -> list[str]:
    if national_team_sports(topic):
        return []
    exclusions = [set(CONTEXT_EXCLUSIONS[word]) for word in context_words(topic)]
    return sorted(set.intersection(*exclusions)) if exclusions else []


def fixed_person_name(topic: str) -> str | None:
    if (person := PERSON_ROLE_QUERY.fullmatch(topic)) and not lexical_role_terms(topic):
        left, right = person.groups()
        for context, name in ((left, right), (right, left)):
            if context.casefold() in PERSON_CONTEXT_WORDS and re.fullmatch(r'[가-힣]{2,4}', name):
                return name
    return None


def query_entities(topic: str) -> list[str]:
    names = [name for name, aliases in ENTITY_ALIASES.items()
             if re.search(title_entity_pattern(aliases), topic, re.IGNORECASE)
             and (name != '기아타이거즈' or not re.search(r'(?<![A-Za-z])KIA(?![A-Za-z])', topic, re.IGNORECASE)
                  or not re.search(r'자동차|기아차|판매|전기차|EV', topic, re.IGNORECASE)
                  or '타이거즈' in topic)]
    if '국가대표' not in names and national_team_sports(topic):
        names.append('국가대표')
    person = fixed_person_name(topic)
    if person and person not in names:
        names.append(person)
    return names


def standardize_question(text: str) -> str:
    text = QUERY_ALIAS_PATTERN.sub(lambda match: prompts.QUERY_ALIASES[match[1]], text)
    text = SAMSUNG_STOCK_ALIAS.sub('삼성전자', text)
    if re.search(r'주가|주식|증시|가격|코스피|코스닥', text):
        text = re.sub(r'왜\s+(?:떨어졌(?:어|나요|나|는지)|하락했(?:어|나요|나|는지))\b', '하락 원인', text)
    return re.sub(r'\b떨어졌(?:어요|어|나요|나|는지)?\b', '하락', text)


def grounded_topic(topic, question: str) -> str:
    """완전한 질문 낱말만 허용한다. 등록된 이름은 띄어쓰기와 별칭을 정규화한다."""
    def words(text):
        for name, aliases in ENTITY_ALIASES.items():
            text = re.sub(title_entity_pattern(aliases), name, text, flags=re.IGNORECASE)
        text = FOUR_DAY_WORKWEEK.sub('주4일제', text)
        return set(TOPIC_WORD.findall(normalize_query_term(text).casefold()))

    question = standardize_question(question)
    if isinstance(topic, str) and re.fullmatch(r'[가-힣A-Za-z0-9\s]+', topic):
        topic = standardize_question(topic)
        workweek = FOUR_DAY_WORKWEEK.search(question)
        if (words(topic) and words(topic) <= words(question)
                and (not workweek or FOUR_DAY_WORKWEEK.search(topic))):
            if workweek:
                topic = FOUR_DAY_WORKWEEK.sub(workweek[0], topic)
            return ' '.join(topic.split())
    return ' '.join(question.split())


def preserve_topic_terms(topic: str, source: str) -> str:
    # 주제에 원문 낱말을 복원할 때는 한글과 영문이 섞인 이름의 표기를 유지한다.
    present = {canonical_keyword(word).casefold() for word in TOPIC_WORD.findall(normalize_query_term(topic))}
    missing = []
    for word in TOPIC_WORD.findall(source):
        normalized = canonical_keyword(normalize_query_term(word)).casefold()
        if normalized not in present and normalized not in TOPIC_FILLER:
            missing.append(word)
            present.add(normalized)
    combined = ' '.join([topic, *missing])
    if len(combined) <= 200:
        return combined
    return ' '.join(source.split())[:200]
QUESTION_TERM_FIELDS = ('required_terms', 'supporting_terms', 'subject_names', 'person_names', 'affiliation_terms')
ASK_SCHEMA = {"type": "object", "properties": {
    "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    **{field: {"type": "array", "items": {"type": "string"}, "maxItems": 8} for field in QUESTION_TERM_FIELDS},
    "person_category": {"enum": [None, *MAJOR_CATEGORIES]},
    "q": {"type": "string", "description": "질문의 핵심 대상과 행동을 모두 보존한 짧은 명사형 기사 제목. 질문형 금지."},
    "date_from": {"type": ["string", "null"], "description": "발행일 시작 YYYY-MM-DD, 조건 없으면 null"},
    "date_to": {"type": ["string", "null"], "description": "발행일 끝 YYYY-MM-DD(포함), 조건 없으면 null"},
    "publisher": {"type": ["string", "null"], "description": "언론사 이름, 조건 없으면 null"}},
    "required": ["keywords", *QUESTION_TERM_FIELDS, "person_category", "q", "date_from", "date_to", "publisher"],
    "additionalProperties": False}
ASK_WEB_SCHEMA = {**ASK_SCHEMA, "properties": {**ASK_SCHEMA["properties"], "resolved_alias": {
    "type": ["object", "null"], "properties": {"term": {"type": "string"}, "canonical": {"type": "string"}},
    "required": ["term", "canonical"], "additionalProperties": False,
}}, "required": [*ASK_SCHEMA["required"], "resolved_alias"]}


class LLMError(Exception):
    """로컬 AI 실패. 내부 응답이나 접속 정보는 공개하지 않는다."""


class LLMUnavailable(LLMError):
    """Ollama에 연결할 수 없다."""


class QuestionConversionError(LLMError):
    """질문을 해석하지 못했고 대체 검색 조건도 없다."""


def llm_model() -> str:
    return os.environ.get("LLM_MODEL", "qwen3:4b")


def llm_think() -> bool:
    return os.environ.get("LLM_THINK", "").strip().lower() in {"1", "true"}


def _cache_get(cache, key):
    with _cache_lock:
        if key not in cache:
            return None
        cache.move_to_end(key)
        # 호출자가 서버 캐시를 변경하지 않게 복사한다.
        return json.loads(json.dumps(cache[key], ensure_ascii=False))


def _cache_put(cache, key, value):
    with _cache_lock:
        cache[key] = json.loads(json.dumps(value, ensure_ascii=False))
        cache.move_to_end(key)
        while len(cache) > 100:
            cache.popitem(last=False)


def ollama_available() -> bool:
    try:
        with urlopen(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/tags", timeout=1) as response:
            models = json.load(response).get("models", [])
        return any(m.get("name") == llm_model() or m.get("model") == llm_model() for m in models)
    except Exception:
        return False


def _ollama_message(messages: list[dict], *, schema: dict | None = None,
                    tools: list[dict] | None = None, deadline: float | None = None,
                    num_predict: int | None = None) -> tuple[dict, float]:
    started = monotonic()
    web_attempt = deadline is not None
    if deadline is None:
        deadline = started + LLM_TIMEOUT
    if not _llm_lock.acquire(timeout=max(0, deadline - started)):
        if web_attempt:
            raise TimeoutError()
        raise LLMError("로컬 AI 대기 시간이 초과되었습니다. 잠시 후 다시 시도하세요.")
    try:
        payload = {"model": llm_model(), "stream": False, "think": llm_think(),
                   "options": {"temperature": 0}, "messages": messages}
        if schema is not None:
            payload["format"] = schema
        if tools:
            payload["tools"] = tools
            num_predict = 128 if num_predict is None else num_predict
        if num_predict is not None:
            payload["options"]["num_predict"] = num_predict
        req = Request(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/chat",
                      data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=remaining_time(deadline)) as response:
            envelope = json.loads(response.read(1_000_000))
        message = envelope["message"]
        if not isinstance(message, dict):
            raise ValueError()
        return message, round((monotonic() - started) * 1000, 1)
    except (TimeoutError, URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
            if web_attempt:
                raise TimeoutError() from None
            raise LLMError("로컬 AI 응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요.") from None
        raise LLMUnavailable("로컬 AI 연결 안 됨 · 기존 필수 키워드와 자연어 주제로 검색하세요.") from None
    except Exception:
        raise LLMError("로컬 AI 응답을 처리하지 못했습니다. 잠시 후 다시 시도하세요.") from None
    finally:
        _llm_lock.release()


def _chat_messages(system: str, data: dict | str) -> list[dict]:
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]


def _chat_json(message: dict) -> dict:
    try:
        # Ollama는 생각 텍스트를 별도의 thinking 필드에 담는다.
        output = json.loads(message["content"])
        if isinstance(output, dict):
            return output
    except (KeyError, TypeError, ValueError):
        pass
    raise LLMError("로컬 AI 응답을 처리하지 못했습니다. 잠시 후 다시 시도하세요.")


def ollama_chat(system: str, data: dict | str, schema: dict, deadline: float | None = None, *,
                num_predict: int | None = None) -> tuple[dict, float]:
    options = {'deadline': deadline} if deadline is not None else {}
    if num_predict is not None:
        options['num_predict'] = num_predict
    try:
        message, elapsed = _ollama_message(_chat_messages(system, data), schema=schema, **options)
    except TimeoutError:
        raise LLMError('로컬 AI 응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요.') from None
    return _chat_json(message), elapsed


def tavily_api_key() -> str:
    if key := os.environ.get('TAVILY_API_KEY', '').strip():
        return key
    try:
        values = dotenv_values(DEFAULT_ENV, encoding='utf-8-sig', interpolate=False)
        return (values.get('TAVILY_API_KEY') or '').strip()
    except (OSError, UnicodeError):
        return ''


def remaining_time(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def tavily_search(query: str, api_key: str, *, timeout: float = TAVILY_TIMEOUT) -> list[dict]:
    request = Request('https://api.tavily.com/search', data=json.dumps({
        'query': query, 'search_depth': 'basic', 'max_results': TAVILY_RESULTS,
        'include_answer': False, 'include_raw_content': False,
    }).encode('utf-8'), headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        results = json.loads(response.read(100_000))['results']
    return [{'title': ' '.join(item['title'].split())[:150],
             'summary': ' '.join(item['content'].split())[:350]}
            for item in results[:TAVILY_RESULTS] if item.get('title') and item.get('content')]


def web_query_conversion(data: dict, api_key: str, candidates: list[str]) -> tuple[dict | None, float, str | None]:
    deadline = monotonic() + WEB_CONVERSION_TIMEOUT
    # 연결·읽기 타임아웃과 별도로 전체 대기를 제한한다. 늦은 응답은 버리고 동시에 한 건만 실행한다.
    if not _web_conversion_lock.acquire(blocking=False):
        raise TimeoutError()
    outcome = Future()
    abandoned = Event()

    def run():
        try:
            try:
                result = _web_query_conversion(data, api_key, candidates, deadline, abandoned)
            finally:
                _web_conversion_lock.release()
        except Exception as exc:
            outcome.set_exception(exc)
        else:
            outcome.set_result(result)

    try:
        Thread(target=run, daemon=True).start()
    except RuntimeError:
        _web_conversion_lock.release()
        raise
    try:
        return outcome.result(timeout=remaining_time(deadline))
    except TimeoutError:
        abandoned.set()
        raise


def _web_query_conversion(data: dict, api_key: str, candidates: list[str],
                          deadline: float, abandoned: Event) -> tuple[dict | None, float, str | None]:
    messages = _chat_messages(prompts.QUERY_WEB_LOOKUP, {**data, 'web_candidates': candidates})
    message, elapsed = _ollama_message(messages, schema=WEB_LOOKUP_SCHEMA, tools=[WEB_SEARCH_TOOL], deadline=deadline)
    tool_calls = message.get('tool_calls')
    if tool_calls:
        function = tool_calls[0]['function']
        if function['name'] != 'web_search':
            raise ValueError('알 수 없는 도구')
        query = function['arguments']['query']
    else:
        query = _chat_json(message)['query']
        if query is None:
            return None, elapsed, None
    query = ' '.join(query.split())
    allowed = {form.casefold() for word in candidates for form in web_word_forms(word) if len(form) >= 2}
    proposed = {form.casefold() for word in QUERY_WORD.findall(query) for form in web_word_forms(word)}
    if not 0 < len(query) <= 200 or allowed.isdisjoint(proposed):
        raise ValueError('잘못된 웹 확인 질의')
    if len(query) <= 198 and query.casefold() in {candidate.casefold() for candidate in candidates}:
        query += ' 뜻'
    if abandoned.is_set():
        raise TimeoutError()
    evidence = tavily_search(query, api_key, timeout=min(TAVILY_TIMEOUT, remaining_time(deadline)))
    if abandoned.is_set() or monotonic() >= deadline:
        raise TimeoutError()
    if not evidence:
        raise ValueError('웹 확인 결과 없음')
    messages[0]['content'] = prompts.QUERY_CONVERSION + ' ' + prompts.QUERY_WEB_RESULT
    messages.extend([
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': 'web_search', 'arguments': {'query': query}}}]},
        {'role': 'tool', 'tool_name': 'web_search', 'content': json.dumps(evidence, ensure_ascii=False)},
    ])
    message, final_ms = _ollama_message(messages, schema=ASK_WEB_SCHEMA, deadline=deadline)
    raw = _chat_json(message)
    if not (isinstance(raw.get('q'), str) and raw['q'].strip() and isinstance(raw.get('keywords'), list)
            and all(isinstance(word, str) for word in raw['keywords'])):
        raise ValueError('잘못된 검색 조건')
    raw['resolved_alias'] = grounded_web_alias(raw.get('resolved_alias'), candidates, evidence)
    remaining_time(deadline)
    return raw, round(elapsed + final_ms, 1), query


def grounded_web_alias(proposed, candidates: list[str], evidence: list[dict]) -> dict | None:
    excerpts = [item['title'] + ' ' + item['summary'] for item in evidence]
    phrase = r'[가-힣A-Za-z0-9]+(?: [가-힣A-Za-z0-9]+){0,3}'
    for term in candidates:
        # 명시적 풀어쓰기가 놓치기 쉬운 일반 기사 질의에서도 약칭의 첫 음절을 확인한다.
        pattern = rf'''(?<![가-힣A-Za-z0-9]){re.escape(term)}['’”"]?\s*\(({phrase})\)'''
        expansions = {match for excerpt in excerpts for match in re.findall(pattern, excerpt)
                      if len(match) <= 30 and ''.join(word[0] for word in match.split()).casefold() == term.casefold()}
        if len(expansions) == 1:
            return {'term': term, 'canonical': expansions.pop()}
        blend = rf'''{re.escape(term)}['’”"]?\s*(?:은|는)\s*['‘“"][^'’”"]{{1,20}}['’”"]\s*(?:과|와)\s*['‘“"]([가-힣]{{2,10}})['’”"](?:의\s*)?(?:조합|합성)'''
        bases = {word for excerpt in excerpts for word in re.findall(blend, excerpt)
                 if term[1:] and word[1:] == term[1:]}
        if len(bases) == 1:
            return {'term': term, 'canonical': bases.pop()}
        if isinstance(proposed, dict) and proposed.get('term') == term:
            canonical = proposed.get('canonical')
            if (isinstance(canonical, str) and canonical.casefold() != term.casefold()
                    and 0 < len(canonical) <= 30 and re.fullmatch(phrase, canonical)
                    and any(term.casefold() in excerpt.casefold() and canonical.casefold() in excerpt.casefold()
                            for excerpt in excerpts)):
                if canonical.endswith('하다') and len(canonical) > 3 and any(
                    re.search(rf'''['‘“"]{re.escape(canonical[:-2])}['’”"]''', excerpt) for excerpt in excerpts
                ):
                    canonical = canonical[:-2]
                return {'term': term, 'canonical': canonical}
    return None


def infer_person_context(topic: str, *, cache_only: bool = False) -> tuple[str, str, float] | None:
    """미등록 직함과 이름으로 된 짧은 질의에서만 대분류 가설을 얻는다."""
    phrase = re.fullmatch(r'([가-힣]{2,12})\s+([가-힣]{2,12})' + CONTEXT_SUFFIX, topic)
    if not phrase or context_words(topic) or query_entities(topic):
        return None
    names = [word for word in phrase.groups() if len(word) <= 4]
    if not names:
        return None
    key = (PROMPT_REVISION, os.environ.get('OLLAMA_URL', ''), llm_model(), llm_think(), phrase.groups())
    cached = _cache_get(_category_cache, key)
    if cached is not None:
        context = cached['context']
        return (context[0], context[1], context[2]) if context else None
    if cache_only:
        return None
    schema = {'type': 'object', 'properties': {
        'name': {'type': ['string', 'null'], 'description': '질문의 직업·직함을 뺀 실제 사람 이름'},
        'category': {'enum': [None, *MAJOR_CATEGORIES]},
    }, 'required': ['name', 'category'], 'additionalProperties': False}
    try:
        raw, elapsed = ollama_chat(
            prompts.PERSON_CONTEXT,
            {'question': ' '.join(phrase.groups()), 'categories': MAJOR_CATEGORIES}, schema)
    except LLMError:
        return None
    name, category = raw.get('name'), raw.get('category')
    context = (name, category, elapsed) if name in names and category in MAJOR_CATEGORIES else None
    _cache_put(_category_cache, key, {'context': context})
    return context


_publisher_cache = (0.0, [])
_publisher_cache_lock = Lock()


def list_publishers() -> list[dict]:
    """DB의 언론사 목록을 읽기 전용으로 조회하고 잠시 캐시한다."""
    global _publisher_cache
    with _publisher_cache_lock:
        now = monotonic()
        if now < _publisher_cache[0]:
            return _publisher_cache[1]
        try:
            with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                                 row_factory=dict_row, autocommit=True, connect_timeout=2,
                                 options='-c default_transaction_read_only=on -c statement_timeout=10000') as conn:
                publishers = conn.execute("""SELECT publisher, count(*) AS article_count
                    FROM article_sources WHERE publisher IS NOT NULL AND publisher <> ''
                    GROUP BY publisher HAVING count(*) >= 10
                    ORDER BY article_count DESC, publisher""").fetchall()
            _publisher_cache = (monotonic() + 300, publishers)
        except Exception:
            publishers = _publisher_cache[1]
            _publisher_cache = (monotonic() + 30, publishers)
        return publishers


def question_publisher_pattern() -> re.Pattern:
    names = sorted({row['publisher'] for row in list_publishers()}, key=lambda name: (-len(name), name))
    patterns = [r'\s*'.join(map(re.escape, name.split())) for name in names if name.strip()]
    alternatives = '|'.join((*patterns, PUBLISHER_WORD))
    return re.compile(rf'(?<!\w)(?:{alternatives})(?:에서의|에서는|에서도|{ALIAS_PARTICLE})?(?!\w)', re.IGNORECASE)


OTHER_PUBLISHERS = '__others__'


def major_publishers(publishers: list[dict]) -> list[str]:
    korean = [row['publisher'] for row in publishers if re.fullmatch(r'[가-힣\s·ㆍ()]+', row['publisher'])]
    major = korean[:30]
    if any(row['publisher'] == '동아일보' for row in publishers):
        if '동아일보' in major:
            major.remove('동아일보')
        major.insert(0, '동아일보')
    return major


def keyword_frequencies(keywords: list[str]) -> dict[str, int]:
    """기존 keyword 인덱스로 존재 확인, 빈도는 50,001건까지만 읽는다."""
    if not keywords:
        return {}
    with psycopg.connect(**connection_options(Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV)))),
                          autocommit=True, row_factory=dict_row, connect_timeout=10,
                          options="-c default_transaction_read_only=on") as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY; SET LOCAL statement_timeout='10s'")
            assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            rows = conn.execute('''
                SELECT word, n FROM unnest(%s::text[]) AS words(word)
                CROSS JOIN LATERAL (SELECT count(*) AS n FROM (
                    SELECT 1 FROM article_keywords WHERE keyword=word LIMIT 50001
                ) AS bounded) AS counts WHERE n > 0
            ''', (keywords,)).fetchall()
            return {row['word']: row['n'] for row in rows}


def web_word_forms(word: str) -> set[str]:
    forms = {word, word.casefold(), word.upper()}
    forms.add(query_verb_stem(word))
    stem = QUERY_PARTICLE_SUFFIX.fullmatch(word)
    if stem and is_query_particle(*stem.groups()):
        forms.update((stem[1], stem[1].casefold(), stem[1].upper()))
    return forms


def known_news_words(words: list[str]) -> set[str]:
    # keyword 동등 조회와 title 정렬 인덱스에서 제한된 범위만 읽는다.
    with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                         row_factory=dict_row, autocommit=True, connect_timeout=2,
                         options='-c default_transaction_read_only=on -c statement_timeout=80') as conn:
        rows = conn.execute("""
            SELECT word FROM unnest(%s::text[]) AS w(word)
            WHERE (SELECT count(*) FROM (
                SELECT 1 FROM article_keywords WHERE keyword=w.word LIMIT %s
            ) AS keywords) = %s OR (SELECT count(*) FROM (
                SELECT title FROM (
                    SELECT title FROM articles WHERE title >= w.word ORDER BY title LIMIT %s
                ) AS titles WHERE starts_with(title, w.word) LIMIT %s
            ) AS matches) = %s
        """, (words, WEB_KNOWN_MIN_ARTICLES, WEB_KNOWN_MIN_ARTICLES,
              WEB_TITLE_SAMPLE, WEB_KNOWN_MIN_ARTICLES, WEB_KNOWN_MIN_ARTICLES)).fetchall()
    return {row['word'] for row in rows}


def unfamiliar_news_words(question: str) -> list[str]:
    topic = QUESTION_DATE_CUE.sub(' ', question)
    topic = re.sub(rf'(?<!\w)(?:{PUBLISHER_WORD})(?:{ALIAS_PARTICLE})?(?!\w)', ' ', topic, flags=re.IGNORECASE)
    registered = (WEB_FILLER | CONTEXT_EXCLUSIONS.keys() | set(MAJOR_CATEGORIES) | SPORT_TITLE_PATTERNS.keys()
                  | {term.casefold() for alias in (*NATIONAL_TEAM_ALIASES, *prompts.QUERY_ALIASES,
                                                   *(a for aliases in ENTITY_ALIASES.values() for a in aliases))
                     for term in QUERY_WORD.findall(alias)})
    candidates = {}
    for word in QUERY_WORD.findall(topic):
        forms = web_word_forms(word)
        if 2 <= len(word) <= 30 and not word.isdecimal() and forms.isdisjoint(registered):
            candidates[word.casefold()] = forms
    if not candidates:
        return []
    known = known_news_words(sorted(form for form in set.union(*candidates.values()) if len(form) >= 2))
    return [word for word, forms in candidates.items() if known.isdisjoint(forms)]


def postprocess_question(raw: dict, lookup=keyword_frequencies) -> dict:
    words = raw.get("keywords")
    q = raw.get("q")
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words) or not isinstance(q, str) or not q.strip():
        raise LLMError("로컬 AI 검색 조건이 비어 있습니다. 수동 검색을 이용하세요.")
    candidates = list(dict.fromkeys(canonical_keyword(normalize_query_term(k)) for w in words[:3]
                                    for k in re.split(r"[,\s]+", w.strip()) if k))[:12]
    if alias := raw.get('resolved_alias'):
        term, canonical = alias['term'], canonical_keyword(alias['canonical'].replace(' ', ''))
        # 두 표기는 동의어다. 필수어에는 DB에서 확인한 한 가지 표기만 적용한다.
        candidates = [canonical, term, *(word for word in candidates if word not in (canonical, term))]
    removed, eligible = [], []
    for keyword in candidates:
        if keyword in ASK_STOPWORDS or len(keyword) > 30:
            removed.append({"keyword": keyword, "reason": "일반어"})
        else:
            eligible.append(keyword)
    lexical = {keyword for keyword in eligible if SHORT_LATIN_KEYWORD.fullmatch(keyword)
               and re.search(title_entity_pattern((keyword,)), q, re.IGNORECASE)}
    short_terms = list(dict.fromkeys(
        canonical_keyword(normalize_query_term(word))
        for word in TOPIC_WORD.findall(raw.get('_term_frequency_source', ''))
        if len(word) >= 2 and not word.isdecimal() and word.casefold() not in CORE_FILLER
    ))
    forms = {word: web_word_forms(word) for word in short_terms} if len(short_terms) == 2 and not alias else {}
    frequency_words = [keyword for keyword in eligible if keyword not in lexical]
    counts = lookup(list(dict.fromkeys([*frequency_words, *sorted(set().union(*forms.values()))])))
    kept, broad = [], []
    for keyword in eligible:
        if keyword not in counts and keyword not in lexical:
            removed.append({"keyword": keyword, "reason": "DB 키워드 없음"})
        elif counts.get(keyword, 0) > 50000:
            removed.append({"keyword": keyword, "reason": "일치 기사 5만 건 초과"})
            broad.append(keyword)
        elif alias and keyword == term and canonical in kept:
            continue
        elif len(kept) < 3:
            kept.append(keyword)
        else:
            removed.append({"keyword": keyword, "reason": "최대 3개 초과"})
    q = q.strip()[:150]
    for keyword in broad:
        if keyword not in q:
            q += " " + keyword
    filters = {}
    for key in ('date_from', 'date_to'):
        try:
            value = raw.get(key)
            filters[key] = date.fromisoformat(value).isoformat() if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
        except ValueError:
            filters[key] = None
    if filters['date_from'] and filters['date_to'] and filters['date_from'] > filters['date_to']:
        filters.update(date_from=None, date_to=None)
    filters['publisher'] = None
    frequencies = {word: max((counts.get(form, 0) for form in variants), default=0)
                   for word, variants in forms.items()}
    return {"keywords": kept, "q": q[:200], "removed_keywords": removed, **filters,
            **({'_term_frequencies': frequencies} if frequencies else {})}


def search_date() -> date:
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def news_digest_category(topic: str) -> str | None:
    words = set(re.findall(r'IT[·_]과학|생활·문화|[가-힣]+|[A-Za-z0-9]+', topic))
    generic = ASK_STOPWORDS | TOPIC_FILLER | {'주요', '최신', '헤드라인', '보여줘', '정리해줘'}
    substantive = {word for word in words if web_word_forms(word).isdisjoint(generic)}
    if not substantive:
        return ''
    if len(substantive) == 1:
        word = next(iter(substantive))
        category = MAJOR_CATEGORY_ALIASES.get(word, word)
        if category in MAJOR_CATEGORIES:
            return category
    return None


def question_weekend_period(ask: str, today: date) -> tuple[str, str, str | None] | None:
    if not (match := CALENDAR_WEEKEND.search(ask)):
        return None
    offset = {'지난': -7, '이번': 0, '다음': 7}[match[1]]
    start = today + timedelta(days=5 - today.weekday() + offset)
    notice = None
    if start > today:
        last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        start = last_sunday - timedelta(days=1)
        notice = f'{match[1]} 주말 전체가 미래여서 가장 최근에 지난 주말의 기사를 검색합니다.'
    return start.isoformat(), (start + timedelta(days=1)).isoformat(), notice


def explicit_question_period(ask: str, today: date) -> tuple[str, str] | None:
    """자주 쓰는 상대 기간과 명시 월은 검색일 기준으로 확정해 모델 계산 오류를 막는다."""
    if match := CALENDAR_RANGE.search(ask):
        first_year, first_month, first_day, last_year, last_month, last_day = match.groups()
        if first_month:
            try:
                year = int(first_year) if first_year else today.year - (int(first_month) > today.month)
                start = date(year, int(first_month), int(first_day))
                end = date(int(last_year or year), int(last_month or first_month), int(last_day))
                if start <= end:
                    return start.isoformat(), end.isoformat()
            except ValueError:
                pass
    compact = re.sub(r'\s+', '', ask)
    if re.search(r'부터|까지|이후|이전|[~～]', compact):
        return None
    start = end = None
    if match := CALENDAR_SUBPERIOD.search(ask):
        year_word, part = match.groups()
        year = today.year + {'작년': -1, '지난해': -1, '재작년': -2, '내년': 1, '후년': 2}.get(year_word, 0)
        if year_word and year_word.endswith('년') and year_word[0].isdigit():
            year = int(year_word[:-1])
        if not 1 <= year <= date.max.year:
            return None
        if part in ('상반기', '하반기'):
            first_month, last_month = (1, 6) if part == '상반기' else (7, 12)
        else:
            last_month = int(part[0]) * 3
            first_month = last_month - 2
        start, end = date(year, first_month, 1), date(year, last_month, monthrange(year, last_month)[1])
    elif match := DAYS_AGO.search(ask):
        days = int(match[1])
        if days < today.toordinal():
            start = end = today - timedelta(days=days)
    elif match := ROLLING_PERIOD.search(ask):
        quantity, unit, native_days = match.groups()
        count = int(quantity) if quantity else 1
        if not count:
            return None
        if unit == '개월' or (unit is None and '달' in match[0]):
            year, month = divmod(today.year * 12 + today.month - 1 - count, 12)
            if year < 1:
                return None
            month += 1
            start = date(year, month, min(today.day, monthrange(year, month)[1]))
        else:
            days = NATIVE_DAY_COUNTS[native_days] if native_days else count * (1 if unit == '일' else 7)
            if days > today.toordinal():
                return None
            start = today - timedelta(days=days - 1)
        end = today
    elif match := RELATIVE_MONTH_DAY.search(ask):
        month = today if match[1] == '이번' else today.replace(day=1) - timedelta(days=1)
        try:
            start = end = month.replace(day=int(match[2]))
        except ValueError:
            return None
    elif '지난달' in compact or '저번달' in compact:
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif '이번달' in compact:
        start, end = today.replace(day=1), today
    elif match := CALENDAR_MONTH_DAY.search(ask):
        year_word, month_word, day_word = match.groups()
        if not year_word and QUESTION_YEAR_CUE.search(ask[:match.start()]):
            return None
        month = int(month_word)
        year = int(year_word) if year_word else today.year - (month > today.month)
        try:
            start = end = date(year, month, int(day_word))
        except ValueError:
            return None
    elif re.search(r'지난해|작년|올해', compact):
        if re.search(r'\d+[월일]|재작년', compact):
            return None
        if '올해' in compact:
            start, end = date(today.year, 1, 1), today
        else:
            start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif weekend := question_weekend_period(ask, today):
        return weekend[0], weekend[1]
    elif match := CALENDAR_WEEK.search(ask):
        monday = today - timedelta(days=today.weekday())
        if match[1] in ('이번', '금'):
            start, end = monday, today
        else:
            start, end = monday - timedelta(days=7), monday - timedelta(days=1)
    elif match := RELATIVE_DAY.search(ask):
        start = end = today + timedelta(days=RELATIVE_DAY_OFFSETS[match[1]])
    elif match := re.search(r'(?<!\d)(\d{4})년(\d{1,2})월(?!\d|부터|까지|이후|이전)', compact):
        # 여러 발행 월을 나열한 질문은 모델 추출값을 검증해 사용한다.
        if len(re.findall(r'\d+월', compact)) == 1:
            year, month = map(int, match.groups())
            try:
                start = date(year, month, 1)
                end = start.replace(day=monthrange(year, month)[1])
            except ValueError:
                pass
    return (start.isoformat(), end.isoformat()) if start and end else None


def recent_question_month(result: dict, ask: str, today: date) -> tuple[str, str] | None:
    """연도 없는 발행 월은 가장 최근 시작한 해당 월을 사용한다."""
    if QUESTION_YEAR_CUE.search(ask) or not (result.get('date_from') and result.get('date_to')):
        return None
    start, end = (date.fromisoformat(result[key]) for key in ('date_from', 'date_to'))
    if start.day != 1 or start.replace(day=monthrange(start.year, start.month)[1]) != end:
        return None
    if not re.search(rf'(?<!\d)0?{start.month}\s*월(?!요일)', ask):
        return None
    year = today.year - (start.month > today.month)
    if start.year == year:
        return None
    start = start.replace(year=year)
    end = start.replace(day=monthrange(year, start.month)[1])
    return start.isoformat(), end.isoformat()


def question_topic_month(ask: str) -> bool:
    return bool(SEASONAL_INTENT.search(ask) and TOPIC_MONTH.search(ask)
                and not re.search(r'\d{4}\s*년|(?:작년|지난해|재작년|내년|후년)\s*\d', ask))


def without_question_dates(text: str, *, keep_month: bool = False) -> str:
    # 날짜에 붙은 조사만 지우고 '일본', '뉴스부터' 같은 주제 표현은 보존한다.
    def replace(match):
        if keep_month and TOPIC_MONTH.fullmatch(match[1]):
            return re.sub(r'\s+', '', match[1])
        return ' '

    text = re.sub(rf'({QUESTION_DATE_CUE.pattern})(?:\s*(?:{DATE_RANGE_SUFFIX}))?', replace, text)
    return re.sub(r'(?<!\S)(?:부터|까지)(?!\S)', ' ', text)


def ground_question_filters(result: dict, ask: str, today: date) -> dict:
    """질문에 근거가 없는 조건을 제거한다. LLM의 null 문자열·무조건 날짜 추측을 차단한다."""
    result = dict(result)
    keep_month = question_topic_month(ask)
    period_text = TOPIC_MONTH.sub(' ', ask) if keep_month else ask
    date_cue = QUESTION_DATE_CUE.search(period_text)
    if not date_cue:
        result.update(date_from=None, date_to=None)
    if period := explicit_question_period(period_text, today) or recent_question_month(result, period_text, today):
        result.update(date_from=period[0], date_to=period[1])
    weekend = question_weekend_period(period_text, today)
    if weekend and weekend[:2] == period:
        if weekend[2]:
            result['date_notice'] = weekend[2]
    elif (result.get('date_from') or '') > today.isoformat():
        result.update(date_from=None, date_to=None)
    elif (result.get('date_to') or '') > today.isoformat():
        result['date_to'] = today.isoformat() if result.get('date_from') else None
    result['publisher'] = None
    q = result['q']
    # 구조화한 조건을 주제 임베딩/AND 키워드에 다시 넣지 않는다.
    if QUESTION_DATE_CUE.search(ask):
        q = without_question_dates(q, keep_month=keep_month)
    day_token = None
    day_match = RELATIVE_MONTH_DAY.search(period_text) or CALENDAR_MONTH_DAY.search(period_text)
    if day_match and period and period[0] == period[1]:
        day = int(day_match.groups()[-1])
        day_token = re.compile(rf'(?<!\w)0?{day}\s*일{CALENDAR_DAY_PARTICLE}')
        q = day_token.sub(' ', q)
    result['keywords'] = [word for word in result['keywords']
                          if (not QUESTION_DATE_CUE.fullmatch(word) or (keep_month and TOPIC_MONTH.fullmatch(word)))
                          and not (day_token and day_token.fullmatch(word))
                          and not re.fullmatch(r'\d{4}|부터|까지', word)]
    result['q'] = ' '.join(q.split()).strip(' ·,~-') or ' '.join(result['keywords'])
    if not result['q']:
        raise LLMError('질문의 검색 주제를 추출하지 못했습니다. 수동 검색을 이용하세요.')
    return result


def without_query_context(text: str) -> str:
    return ' '.join(QUERY_CONTEXT_FILLER.sub(' ', text).split())


def required_keyword_terms(params: dict, supporting: list[str]) -> list[str]:
    context = {word.casefold() for word in supporting}
    return [word for word in params['keywords']
            if web_word_forms(word).isdisjoint(context) and without_query_context(word)]


def ground_query_term(word: str, source: str, names=()) -> str | None:
    word = normalize_query_term(word, names)
    if not word:
        return None
    canonical = canonical_keyword(word)
    forms = (*ENTITY_ALIASES.get(canonical, (canonical,)), COMPANY_ALIASES.get(canonical, canonical))
    pattern = title_entity_pattern(dict.fromkeys((word, *forms)), ignore_spacing=True)
    source = normalize_query_term(source, names)
    restored = None
    for match in re.finditer(rf'(?<![가-힣A-Za-z0-9])({pattern})({ALIAS_PARTICLE})?(?![가-힣A-Za-z0-9])',
                             source, re.IGNORECASE):
        if is_query_particle(match[1], match[2]):
            return word
        # 모델이나 이전 검색 조건이 잘라낸 이름은 질문에 실제로 있는 표기로 복원한다.
        restored = restored or match[0]
    return restored


def normalize_required_terms(required: list[str], supporting: list[str], source: str, names=()) -> list[str]:
    """질문에 있는 필수어를 보존하고, 겹치는 구절을 정리한다."""
    required = [grounded for word in required if (grounded := ground_query_term(word, source, names))]
    for compound, aliases in QUERY_COMPOUND_ALIASES.items():
        pattern = title_entity_pattern((compound, *aliases), ignore_spacing=True)
        if re.search(rf'(?<!\w)(?:{pattern})(?:{ALIAS_PARTICLE})?(?!\w)', source, re.IGNORECASE):
            forms = {compound, *aliases, *(part for alias in aliases for part in alias.split())}
            required = [compound if word in forms else word for word in required]
    required = list(dict.fromkeys(required))
    query_words = TOPIC_WORD.findall(normalize_query_term(source, names).casefold())
    terms = set(required) | set(supporting)
    normalized = {}
    for word in required:
        parts = word.casefold().split()
        if len(parts) > 1 and (parts == query_words or any(
            re.search(rf'(?<!\w){re.escape(form)}(?:{ALIAS_PARTICLE})?(?!\w)', word, re.IGNORECASE)
            for term in terms if term.casefold() != word.casefold() for form in core_keyword_forms(term)
        )):
            continue
        normalized.setdefault(word.casefold(), word)
    return list(normalized.values())


def local_event_terms(source: str) -> list[str]:
    """일반 행동·행사어만 붙은 지명은 검색 대상이다."""
    places, actions = [], []
    for word in TOPIC_WORD.findall(without_query_context(normalize_query_term(source))):
        if word in CORE_FILLER:
            continue
        if action := LOCAL_EVENT_WORD.fullmatch(word):
            actions.append(action[1])
        elif (place := LOCAL_PLACE_WORD.fullmatch(word)) and place[1] not in NON_PLACE_WORDS:
            places.append(place[1])
        else:
            return []
    return [*places, *actions] if places and actions else []


def question_term_partition(raw: dict, params: dict, source: str, *, frequencies: dict[str, int] | None = None) -> dict:
    """질문 표현을 검증하고 검색 대상과 등록 관점어를 필수어로 보정한다."""
    normalized_source = normalize_query_term(source)

    def in_question(pattern, particles=ALIAS_PARTICLE):
        return re.search(rf'(?<!\w)(?:{pattern})(?:{particles})?(?!\w)', normalized_source, re.IGNORECASE)

    def name_in_question(name):
        short = re.fullmatch(r'[가-힣]', name)
        return in_question(title_entity_pattern((name,), ignore_spacing=not short),
                           SHORT_NAME_PARTICLE if short else ALIAS_PARTICLE)

    def grounded_terms(field, names=()):
        words = raw.get(field)
        if not isinstance(words, list) or len(words) > 8:
            return None
        terms = []
        for word in words:
            if not isinstance(word, str) or not re.fullmatch(r'[가-힣A-Za-z0-9 ]{1,30}', word.strip()):
                return None
            word = without_query_context(word)
            if not word:
                continue
            if grounded := ground_query_term(word, source, names):
                word = canonical_keyword(grounded)
                if web_word_forms(word).isdisjoint(CORE_FILLER):
                    terms.append(word)
        return list(dict.fromkeys(terms))

    member_groups, named_members = [], []
    # 등록 멤버는 모델이 이름과 소속을 바꾸거나 한 글자 이름을 누락해도 복원한다.
    for aliases, members in GROUP_MEMBERS.items():
        groups = [alias for alias in aliases if in_question(title_entity_pattern((alias,)))]
        if groups and (matched := [name for name in members if name_in_question(name)]):
            named_members.extend(matched)
            member_groups.extend(groups)
    non_people = (PERSON_CONTEXT_WORDS | PERSON_TITLE_WORDS | QUERY_DOMAIN_WORDS | QUERY_TIME_WORDS
                  | SEASON_CONTEXT_WORDS | PERSON_DESCRIPTORS | NON_PERSON_ENTITIES
                  | {'ai', '육군', '해군', '공군', '해병대', '경찰청'})
    people = [name for name in grounded_terms('person_names', named_members) or []
              if re.fullmatch(r'[가-힣]{1,5}|[A-Za-z][A-Za-z0-9]{0,29}', name)
              and web_word_forms(name).isdisjoint(non_people) and name_in_question(name)]
    people.extend(name for name in sorted(TITLE_REQUIRED_PEOPLE) if name_in_question(name))
    if person := fixed_person_name(source):
        people.append(person)
    people = list(dict.fromkeys((*people, *named_members)))
    required = grounded_terms('required_terms', people)
    supporting = grounded_terms('supporting_terms', people)
    valid = bool(required and supporting is not None
                 and {word.casefold() for word in required}.isdisjoint(word.casefold() for word in supporting)
                 and any(not TOPIC_MONTH.fullmatch(word) and not word.isdecimal() for word in required))
    legacy = {**params, 'q': grounded_topic(params['q'], source), 'person_names': people,
              'keywords': [grounded for word in params['keywords'] if (grounded := ground_query_term(word, source, people))]}
    aspects = list(topic_aspects(normalized_source))
    aspects.extend(aspect for aspect in (*QUERY_EVENT_ASPECTS, *(PERSON_EVENT_ASPECTS if people else ()))
                   if in_question(aspect))
    subjects = [*query_entities(source), *people, *(grounded_terms('subject_names', people) or [])]
    # 모델이 복합 대상에서 빠뜨리기 쉬운 등록 상호와 그룹명은 원문에서 확인한다.
    # 약칭 사전에 등록된 기업·기관명은 모델이 보조어로 분류해도 대상(필수어)으로 고정한다.
    # 정식명·약칭 쌍은 하나만 남긴다(모델 필수어 '한국전력' + 질문 '한전' → '한국전력').
    seen_entities = {COMPANY_ALIASES.get(word, word) for word in (required or [])} | set(required or [])
    named_entities = []
    for name in dict.fromkeys(('현대차', *sorted(JOINT_QUERY_SUBJECTS), *COMPANY_ALIASES)):
        if in_question(re.escape(name)) and not {name, COMPANY_ALIASES.get(name, name)} & seen_entities - {name}:
            named_entities.append(name)
            seen_entities |= {name, COMPANY_ALIASES.get(name, name)}
    subjects.extend(named_entities)
    affiliations = [*(grounded_terms('affiliation_terms', people) or []), *member_groups]
    ai = in_question(f'({title_entity_pattern(("AI",))})')
    technology = [ai[1]] if ai else []
    if technology and in_question('교육'):
        technology.append('교육')
    affiliation_keys = ({word.casefold() for word in affiliations}
                        - {word.casefold() for word in (*people, *aspects, *technology)})
    if JOINT_QUERY_SUBJECTS.issubset(named_entities):
        affiliation_keys.difference_update(JOINT_QUERY_SUBJECTS)
    if not any(word.casefold() not in affiliation_keys for word in subjects):
        affiliation_keys.clear()
    affiliations = [word for word in affiliations if word.casefold() in affiliation_keys]
    protected = [*subjects, *aspects, *technology]
    if FOUR_DAY_WORKWEEK.search(source):
        protected.append('주4일제')
    cyber_rekka = in_question(r'사이버\s*렉카')
    if cyber_rekka:
        protected.append('사이버렉카')
    classified = {word.casefold() for word in (*(required or []), *(supporting or []))}
    protected.extend(word for word in TOPIC_WORD.findall(source)
                     if re.fullmatch(r'[A-Z][A-Z0-9]{1,7}', word)
                     and word.casefold() not in classified and canonical_keyword(word) == word)
    # '서울 공존'과 장소에 붙은 활용형 질문은 행동을 주제로 쓴다.
    location_actions = dict.fromkeys(stem for word in TOPIC_WORD.findall(source)
                                     if (stem := query_verb_stem(word)) != word or stem == '공존')
    locations = [word for word in sorted(QUERY_LOCATION_WORDS) if location_actions and in_question(word)]
    if locations and {part for word in (required or []) for part in word.split()} <= location_actions.keys() | set(locations):
        protected.extend(location_actions)
    else:
        locations = []
    required = [*(required if valid else core_terms(legacy)), *protected]
    supporting = supporting if valid else []
    # 시기·형식·장소를 명시하는 문법이 확인되면 모델이 생략한 보조어도 복원한다.
    context = [*affiliations, *locations]
    generic = QUERY_TIME_WORDS.copy()
    if cyber_rekka:
        generic.add('처벌')
    if aspects:
        generic.update(QUERY_DOMAIN_WORDS)
    if people:
        generic.update(PERSON_TITLE_WORDS | PERSON_DESCRIPTORS)
    seasons = {word for word in SEASON_CONTEXT_WORDS if in_question(word)}
    if seasons and any(web_word_forms(word).isdisjoint(seasons) for word in core_terms(legacy)):
        generic.update(seasons)
    modifiers = [word for word in sorted(generic) if in_question(word)]
    # '기관 분야'나 '인물 직함'으로 붙어 들어온 필수어에서도 대상은 보존한다.
    modifier_keys = {word.casefold() for word in (*modifiers, *context)}
    required = [' '.join(part for part in word.split() if web_word_forms(part).isdisjoint(modifier_keys))
                for word in required]
    if seasons & generic and not any(required):
        required = [word for word in core_terms(legacy) if web_word_forms(word).isdisjoint(modifier_keys)]
    if any(required):
        context.extend(modifiers)
    required = list(filter(None, required))
    if question_topic_month(source):
        context.extend(match[0] for match in TOPIC_MONTH.finditer(source))
        if re.search(r'(?<!\w)여행(?:\s|$)', source):
            required.append('여행')
    if season := re.search(r'(?<!\w)(봄|여름|가을|겨울)\s+(여행|휴가)(?:\s+(코스))?(?!\w)', source):
        required.append(season[2])
        if season[3]:
            context.append(season[3])
    if location := re.search(r'(?<!\w)([가-힣]+)에서\s+(?:열리는|개최되는)\s+마라톤(?:\s+대회)?(?!\w)', source):
        required.append('마라톤')
        context.append(location[1])
        if location[0].endswith('대회'):
            context.append('대회')
    if holiday := HOLIDAY_CONTEXT.search(source):
        holiday_words = [holiday[1], '연휴']
        other_terms = [word for word in core_terms(legacy) if set(word.split()).isdisjoint(holiday_words)]
        if other_terms:
            context.extend(holiday_words)
            if not any(set(word.split()).isdisjoint(holiday_words) for word in required):
                required.extend(other_terms)
                valid = False
    # 모델이 검색 키워드를 분할에서 빠뜨려도 명시적으로 보조어인 키워드만 필수에서 뺀다.
    required.extend(required_keyword_terms(legacy, [*supporting, *context]))
    roles = [word for word in required if not web_word_forms(word).isdisjoint(PERSON_CONTEXT_WORDS)]
    if roles:
        if all(word in roles for word in required):
            required.extend(word for word in required_keyword_terms(legacy, context) if word not in roles)
        if any(word not in roles for word in required):
            context.extend(roles)
    context_keys = {word.casefold() for word in context}
    required = [word for word in required if context_keys.isdisjoint(word.casefold().split())
                and word.casefold() not in context_keys]
    supporting = [grounded for word in (*supporting, *context) if (grounded := ground_query_term(word, source, people))]
    local_terms = local_event_terms(source)
    normalized = normalize_required_terms([*local_terms, *required], supporting, source, people)
    if not normalized:
        support_keys = {word.casefold() for word in supporting}
        normalized = normalize_required_terms([word for word in core_terms(legacy) if word.casefold() not in support_keys],
                                              supporting, source, people)
        valid = False
    if len(TOPIC_WORD.findall(normalized_source)) == 2 and in_question('경찰청') and in_question('범죄'):
        # 기관과 일반 분야만 제시한 질문은 해당 기관을 검색 대상으로 삼는다.
        normalized, supporting = ['경찰청'], ['범죄']
    elif frequencies and not local_terms:
        specific, general = sorted(frequencies, key=frequencies.__getitem__)
        specific_key, general_key = specific.casefold(), general.casefold()
        selected = {word.casefold() for word in normalized}
        protected_general = {word.casefold() for word in (*people, *aspects, *named_entities, *technology)}
        if (0 < frequencies[specific] * 10 <= frequencies[general] and frequencies[general] >= 25000
                and general_key in selected and selected <= {specific_key, general_key}
                and general_key not in protected_general and specific_key not in context_keys):
            normalized = [specific]
            supporting.append(general)
    required = normalized
    required_keys = {word.casefold() for word in required}
    required_keys.update(part.casefold() for word in required for alias in QUERY_COMPOUND_ALIASES.get(word, ())
                         for part in alias.split())
    return {
        'required_terms': required,
        'supporting_terms': list(dict.fromkeys(word for word in supporting if word.casefold() not in required_keys
                                               and required_keys.isdisjoint(word.casefold().split()))),
        'person_names': people,
        'term_source': 'model' if valid else 'legacy',
    }


def convert_question(ask: str, today: date | None = None) -> dict:
    if not isinstance(ask, str) or not ask.strip() or len(ask) > 500:
        raise SearchError("질문은 1~500자로 입력하세요.")
    today = today or search_date()
    publishers = question_publisher_pattern()
    original_words = set(TOPIC_WORD.findall(ask.casefold()))
    ask = ' '.join(publishers.sub(' ', ask).split())
    if not ask:
        raise LLMError('질문의 검색 주제를 추출하지 못했습니다. 수동 검색을 이용하세요.')
    question = standardize_question(ask)
    keep_month = question_topic_month(ask)
    source = ' '.join(without_question_dates(question, keep_month=keep_month).split()).strip(' ·,~-')
    if (period := explicit_question_period(ask, today)) and period[0] <= today.isoformat():
        if news_digest_category(source) is not None:
            digest = ground_question_filters({"keywords": [], "q": source[:200] or '뉴스', "removed_keywords": []}, ask, today)
            return {**digest, "llm_ms": 0, "cached": False,
                    "web_query": None, "web_reason": "기간과 뉴스 분류 확인",
                    "required_terms": [], "supporting_terms": [], "person_names": [], "term_source": "legacy"}
    if role_terms := lexical_role_terms(source):
        result = ground_question_filters({"keywords": role_terms, "q": source, "removed_keywords": []}, ask, today)
        return {**result, "required_terms": role_terms, "supporting_terms": [], "person_names": [],
                "term_source": "legacy", "llm_ms": 0, "cached": False, "web_query": None,
                "web_reason": "직함 약칭과 등록 관점어"}
    bare_person = question in REGISTERED_PERSON_NAMES
    names = [question] if bare_person else query_entities(question)
    if not names and SHORT_LATIN_KEYWORD.fullmatch(question):
        names = [question]
    if names and not QUESTION_DATE_CUE.search(ask):
        params = {"keywords": names, "q": '' if bare_person else grounded_topic(None, question)[:200]}
        return {**params, **question_term_partition({}, params, question), "removed_keywords": [],
                "date_from": None, "date_to": None, "publisher": None, "llm_ms": 0, "cached": False,
                "web_query": None, "web_reason": "등록된 대상"}
    api_key = tavily_api_key()
    key = (QUERY_REVISION, today.isoformat(), os.environ.get("OLLAMA_URL", ""), llm_model(), llm_think(),
           ask, bool(api_key))
    cached = _cache_get(_ask_cache, key)
    if cached is not None:
        return {**cached, "cached": True}
    data = {'question': question, 'search_date': today.isoformat()}
    web_query, web_failed, web_gate_ms = None, False, 0
    web_reason, candidates = '웹 검색 키 없음', []
    if api_key:
        started = perf_counter()
        try:
            candidates = unfamiliar_news_words(question)
            web_reason = '알려진 낱말'
        except (psycopg.Error, OSError):
            web_reason, web_failed = '낱말 빈도 확인 실패', True
        web_gate_ms = round((perf_counter() - started) * 1000, 1)
    raw, web_wait_ms = None, 0
    if candidates:
        started = perf_counter()
        try:
            raw, elapsed, web_query = web_query_conversion(data, api_key, candidates)
            web_reason = None if web_query else '도구 호출 생략'
        except TimeoutError:
            web_reason, web_failed = '시간 초과', True
        except (LLMError, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            web_reason, web_failed = '웹 확인 실패', True
        web_wait_ms = round((perf_counter() - started) * 1000, 1)
    if raw is None:
        raw, elapsed = ollama_chat(prompts.QUERY_CONVERSION, data, ASK_SCHEMA)
        elapsed = round(elapsed + web_wait_ms, 1)
    try:
        source_words = set(TOPIC_WORD.findall(source.casefold()))
        removed_words = ((original_words | set(TOPIC_WORD.findall(question.casefold())))
                         - source_words)
        # 숫자가 붙은 주제어에서 모델이 잘라낸 숫자나 접미부를 독립 키워드로 쓰지 않는다.
        numeric_parts = {part for match in re.finditer(r'(?<!\w)(\d+)([가-힣]+)(?!\w)', source)
                         for part in match.groups()}
        removed_words.update(numeric_parts - source_words)
        # 등록 별칭을 표준화해도 모델이 반환한 별칭 자체는 후보에 남긴다.
        removed_words.difference_update(prompts.QUERY_ALIASES)

        def clean_term(word):
            if not isinstance(word, str):
                return word
            word = without_question_dates(publishers.sub(' ', word), keep_month=keep_month)
            return ' '.join(part for part in word.split() if part.casefold() not in removed_words)

        alias = raw.get('resolved_alias') if web_query else None
        if alias and any(clean_term(alias[key]) != alias[key] for key in ('term', 'canonical')):
            alias = None
        if isinstance(raw.get('q'), str):
            raw = {**raw, 'resolved_alias': alias, 'q': grounded_topic(clean_term(raw['q']), source)}
        for field in ('keywords', *QUESTION_TERM_FIELDS):
            if isinstance(raw.get(field), list):
                raw = {**raw, field: [word for item in raw[field] if (word := clean_term(item)) != '']}
        if isinstance(raw.get('keywords'), list):
            candidates = []
            for word in raw['keywords']:
                if not isinstance(word, str):
                    candidates.append(word)
                elif grounded := ground_query_term(without_query_context(word), source):
                    candidates.append(grounded)
            raw['keywords'] = candidates
        interpreted = postprocess_question({**raw, '_term_frequency_source': source})
        frequencies = interpreted.pop('_term_frequencies', None)
        interpreted['q'] = grounded_topic(interpreted['q'], source)
        interpreted = ground_question_filters(interpreted, ask, today)
        topic = preserve_topic_terms(interpreted['q'], source)
        if alias and (canonical := alias['canonical']) not in topic:
            prefix = ' '.join((*([alias['term']] if alias['term'] not in topic else []), canonical))
            topic = f'{topic[:199 - len(prefix)].rstrip()} {prefix}'
        interpreted['q'] = topic
        if names:
            interpreted['keywords'] = names
        short_words = [word for word in QUERY_WORD.findall(source) if word not in WEB_FILLER]
        if not names and len(short_words) == 1 and SHORT_LATIN_KEYWORD.fullmatch(short_words[0]):
            interpreted.update(keywords=short_words, q=short_words[0])
        if (interpreted['date_from'] or interpreted['date_to']) and news_digest_category(topic) is not None:
            interpreted['keywords'] = []
        elif focus := topic_title_focus(topic):
            # 쟁점은 제목 순위에 반영해 모델이 뽑은 여러 단어의 교집합으로 후보가 줄지 않게 한다.
            _, context, _, detail = focus
            interpreted['keywords'] = [word for word in interpreted['keywords']
                                       if not re.fullmatch(rf'{context}|{detail}', word, re.IGNORECASE)]
    except LLMError:
        raise
    except Exception:
        raise LLMError('질문 해석을 처리하지 못했습니다. 수동 검색 조건을 사용합니다.') from None
    result = {**interpreted, "llm_ms": elapsed, "cached": False, "web_query": web_query,
              "web_reason": web_reason, "web_gate_ms": web_gate_ms, "web_wait_ms": web_wait_ms,
              **({'web_alias': alias} if alias else {})}
    if (result['date_from'] or result['date_to']) and news_digest_category(topic) is not None:
        result.update(required_terms=[], supporting_terms=[], person_names=[], term_source='legacy')
    else:
        result.update(question_term_partition(raw, interpreted, source, frequencies=frequencies))
        required = result['required_terms']
        required_by_key = {word.casefold(): word for word in required}
        keywords = [required_by_key[word.casefold()] for word in required_keyword_terms(result, result['supporting_terms'])
                    if word.casefold() in required_by_key]
        if not keywords and result['supporting_terms']:
            keywords = [name for name in result['person_names'] if name in required][:1]
        if not keywords or 'ai' in required_by_key:
            # 보정한 주제도 응답 키워드에 남겨 검색 조건과 핵심 기사 집계가 같은 주제를 가리키게 한다.
            keywords = list(dict.fromkeys([*keywords, *required]))[:3]
        result['keywords'] = keywords
    category = raw.get('person_category')
    if (category in MAJOR_CATEGORIES and len(result['person_names']) == 1 and not names and not context_words(topic)
            and set(result['required_terms']) == set(result['person_names'])
            and not QUESTION_DATE_CUE.search(ask)
            and re.fullmatch(r'[가-힣]{2,12}\s+[가-힣]{2,12}' + CONTEXT_SUFFIX, question)):
        result.update(keywords=result['person_names'], target_category=category)
    if not web_failed:
        _cache_put(_ask_cache, key, result)
    return result


def timeline_candidates(result: dict) -> list[dict]:
    if 'timeline_candidates' in result:
        return result['timeline_candidates']
    events = result['events']
    core = [event for event in events if event.get('tier') == 'core']
    events = core or result.get('related_events') or events
    candidates = events if core or 'related_events' in result or result.get('monthly_event_selection') else events[:TIMELINE_CANDIDATES]
    return sorted(candidates,
                  key=lambda e: (e["date_from"], e["date_to"], e["group_id"]))


def timeline_subject_pattern(params: dict) -> str:
    person = fixed_person_name(params.get('q', ''))
    return title_entity_pattern(tuple(dict.fromkeys(
        alias for term in params['keywords']
        if term in TEAM_NAMES or term in REGISTERED_PERSON_NAMES or term == person
        for alias in ENTITY_ALIASES.get(term, (term,)))))


def keyword_timeline_query(params: dict, vector: str | None) -> tuple[str, tuple]:
    """전체 AND 집합에서 주제 유사도와 묶음 전체 보도량으로 후보를 고른다."""
    embedding = ("LEFT JOIN LATERAL (SELECT embedding FROM article_embeddings "
                 "WHERE article_id=a.id OFFSET 0) e ON TRUE" if vector else '')
    score = '1-(e.embedding <=> %s::vector)' if vector else 'NULL::double precision'
    source, source_values = keyword_source(params['keywords'], params.get('q', ''))
    signals, aspect_pattern = timeline_title_signals(params) if vector else (None, '')
    signal_columns = ', ' + TITLE_SIGNALS_SQL if signals else ''
    mismatch_sql, mismatch_values = category_mismatch_flag(params) if vector else ('', ())
    mismatch_column = f', {mismatch_sql} AS category_mismatch' if mismatch_sql else ''
    rank = TITLE_RANK_SQL if signals else 'score'
    if mismatch_sql:
        rank = category_rank_sql(rank)
    ranked_members = (f', ranked_members AS (SELECT *, {rank} AS rank_score FROM members)'
                      if signals or mismatch_sql else '')
    rank_column = ', max(rank_score) AS rank_score' if ranked_members else ''
    similarity_order = 'rank_score' if ranked_members else 'score'
    aspect = bool(signals and aspect_pattern)
    match_order = ('context_match DESC, ' if aspect else '') + (similarity_order if mismatch_sql or aspect else 'score')
    aspect_column = ', bool_or(context_match) AS aspect_match' if aspect else ''
    aspect_order = 'aspect_match DESC, ' if aspect else ''
    match_category = ", 'category_mismatch', category_mismatch" if mismatch_sql else ''
    subject = timeline_subject_pattern(params)
    subject_order = 'subject_headlines DESC, ' if subject else ''
    coverage_order = f'{subject_order}{aspect_order}publisher_count DESC, article_count DESC, latest_match DESC, group_id DESC'
    match_subject = 'a.title ~* (SELECT pattern FROM subject)'
    subject_column = f', {match_subject} AS subject_match' if subject else ''
    matched_subject = ', bool_or(subject_match) AS singleton_subject' if subject else ''
    group_subject = (f''', COALESCE(max(a.id) FILTER (WHERE g.is_representative), min(a.id))
                = ANY(array_agg(a.id) FILTER (WHERE {match_subject}))
                OR count(*) FILTER (WHERE {match_subject}) * 2 > count(*) AS subject_headlines''' if subject else '')
    event_subject = (', COALESCE(CASE WHEN grouped THEN totals.subject_headlines ELSE singleton_subject END, FALSE)'
                     ' AS subject_headlines' if subject else '')
    if vector:
        selection = f'''
            , ranked AS (
                SELECT event_totals.*,
                       row_number() OVER (ORDER BY {subject_order}{aspect_order}{similarity_order} DESC NULLS LAST,
                                         article_count DESC, latest_match DESC, group_id DESC) AS similarity_rank,
                       row_number() OVER (ORDER BY {coverage_order}) AS coverage_rank
                FROM event_totals
            )
            SELECT * FROM ranked WHERE similarity_rank <= %s OR coverage_rank <= %s
            ORDER BY similarity_rank
        '''
        limits = (KEYWORD_TIMELINE_CANDIDATES, TIMELINE_COVERAGE_CANDIDATES)
    else:
        selection = f'SELECT * FROM event_totals ORDER BY {coverage_order} LIMIT %s'
        limits = (KEYWORD_TIMELINE_CANDIDATES,)
    conditions, filter_values = timeline_article_filter(params)
    subject_cte = 'subject AS (SELECT %s::text AS pattern), ' if subject else ''
    return f'''
        WITH {subject_cte}matched AS MATERIALIZED ({source}),
        members AS MATERIALIZED (
            SELECT a.id, a.published_date, COALESCE(g.group_id, a.id) AS group_id,
                   g.group_id IS NOT NULL AS grouped, {score} AS score{signal_columns}{subject_column}{mismatch_column}
            FROM matched m JOIN articles a ON a.id=m.article_id
            LEFT JOIN LATERAL (
                SELECT group_id FROM article_groups WHERE article_id=a.id OFFSET 0
            ) g ON TRUE {embedding}
            WHERE TRUE {conditions}
        ) {ranked_members}
        , matched_groups AS (
            SELECT group_id, grouped, count(*) AS matched_count,
                   min(published_date) AS date_from, max(published_date) AS date_to,
                   max(published_date) AS latest_match, max(score) AS score{rank_column}{matched_subject}{aspect_column},
                   (array_agg(jsonb_build_object('id', id, 'published_date', published_date, 'score', score{match_category})
                       ORDER BY {match_order} DESC NULLS LAST, published_date DESC, id DESC))[1:3] AS matches
            FROM {'ranked_members' if ranked_members else 'members'} GROUP BY grouped, group_id
        )
        , event_totals AS (
            SELECT matched_groups.*, CASE WHEN grouped THEN totals.article_count ELSE 1 END AS article_count,
                   CASE WHEN grouped THEN totals.publisher_count ELSE
                       (SELECT count(*) FROM article_sources s WHERE s.article_id=matched_groups.group_id)
                   END AS publisher_count
                   {event_subject}
            FROM matched_groups
            LEFT JOIN LATERAL (
                SELECT count(*) AS article_count, count(DISTINCT s.publisher) AS publisher_count{group_subject}
                FROM article_groups g JOIN articles a ON a.id=g.article_id
                LEFT JOIN article_sources s ON s.article_id=a.id
                WHERE grouped AND g.group_id=matched_groups.group_id {conditions}
            ) totals ON TRUE
        )
        {selection}
    ''', (*((subject,) if subject else ()), *source_values, *((vector,) if vector else ()),
          *(signals or ()), *mismatch_values, *filter_values, *filter_values, *limits)


def keyword_timeline_candidates(params: dict) -> list[dict]:
    vector = query_embedding(params['q']) if params['q'] else None
    subject = timeline_subject_pattern(params)
    _, aspect = timeline_title_signals(params)
    subject_rep = (''', (array_agg(a.id ORDER BY g.is_representative DESC NULLS LAST,
                            a.published_date DESC, a.id DESC) FILTER (WHERE a.title ~* %s))[1] AS subject_id'''
                   if subject else '')
    try:
        with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                              autocommit=True, row_factory=dict_row, connect_timeout=10,
                              options='-c default_transaction_read_only=on') as conn:
            conn.read_only = True
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute("SET LOCAL statement_timeout='10s'; SET LOCAL jit=off; SET LOCAL work_mem='32MB'")
                params = with_article_duplicates(conn, params)
                conditions, filter_values = timeline_article_filter(params)
                sql, values = keyword_timeline_query(params, vector)
                events = conn.execute(sql, values).fetchall()
                groups = [e['group_id'] for e in events if e['grouped']]
                summaries = conn.execute(f'''
                    SELECT wanted.group_id, summary.* FROM unnest(%s::bigint[]) wanted(group_id)
                    CROSS JOIN LATERAL (
                        SELECT min(a.published_date) AS date_from, max(a.published_date) AS date_to,
                               max(g.summary) AS summary,
                               COALESCE(max(g.article_id) FILTER (WHERE g.is_representative), min(a.id))
                                   AS representative_id,
                               array_agg(DISTINCT s.publisher ORDER BY s.publisher)
                                   FILTER (WHERE s.publisher IS NOT NULL) AS publishers{subject_rep}
                        FROM article_groups g JOIN articles a ON a.id=g.article_id
                        LEFT JOIN article_sources s ON s.article_id=a.id
                        WHERE g.group_id=wanted.group_id {conditions}
                    ) summary
                ''', (groups, *((subject,) if subject else ()), *filter_values)).fetchall() if groups else []
                by_group = {r['group_id']: r for r in summaries}
                for event in events:
                    if event['grouped']:
                        event.update(by_group[event['group_id']])
                        subject_id = event.pop('subject_id', None)
                        if event.get('subject_headlines') and subject_id:
                            event['representative_id'] = subject_id
                    else:
                        event['representative_id'] = event['group_id']
                    if 'category_mismatch' in event['matches'][0]:
                        first = event['matches'][0]
                        event['representative_id'] = first['id']
                        event['category_mismatch'] = first['category_mismatch']
                ids = sorted({e['representative_id'] for e in events} |
                             {m['id'] for e in events for m in e['matches']})
                details = conn.execute(f'''
                    SELECT a.id, a.title, a.published_date, a.url, s.publisher,
                           ARRAY(SELECT DISTINCT ({NORMALIZED_MAJOR_SQL}) || COALESCE(' > ' || NULLIF(btrim(c.minor), ''), '')
                                 FROM article_categories c WHERE c.article_id=a.id ORDER BY 1) AS categories
                    FROM articles a LEFT JOIN article_sources s ON s.article_id=a.id
                    WHERE a.id=ANY(%s::bigint[])
                ''', (ids,)).fetchall() if ids else []
                by_id = {r['id']: r for r in details}
                for event in events:
                    if event.get('aspect_match') and not re.search(
                            aspect, by_id[event['representative_id']]['title'], re.IGNORECASE):
                        event.update(representative_id=event['matches'][0]['id'], summary=None)
                    event['representative'] = by_id[event['representative_id']]
                    event['matches'] = [{**m, **by_id[m['id']]} for m in event['matches']]
                    event['publishers'] = event.get('publishers') or (
                        [event['representative']['publisher']] if event['representative']['publisher'] else [])
        # SQL 순위를 보존한다. 병합 함수가 날짜순으로 정렬하며 동률 선택은 이 순위를 쓴다.
        return _json_values(events)
    except Exception:
        raise SearchError('타임라인 후보 조회 실패: DB 연결·읽기 권한을 확인하세요.') from None


def normalized_vector(vector) -> list[float] | None:
    if vector is None:
        return None
    values = json.loads(vector) if isinstance(vector, str) else list(vector)
    norm = math.sqrt(sum(float(v) ** 2 for v in values))
    return [float(v) / norm for v in values] if norm and math.isfinite(norm) else None


def timeline_query_min_cos() -> float:
    """kw+q 대표 기사 기준. 환경 변수 변경은 다음 요청과 캐시에 즉시 반영한다."""
    try:
        value = float(os.environ.get('TIMELINE_Q_MIN_COS', TIMELINE_Q_MIN_COS))
    except ValueError:
        raise SearchError('TIMELINE_Q_MIN_COS는 -1~1 사이 유한한 숫자여야 합니다.') from None
    if not math.isfinite(value) or not -1 <= value <= 1:
        raise SearchError('TIMELINE_Q_MIN_COS는 -1~1 사이 유한한 숫자여야 합니다.')
    return value


def timeline_representative_vectors(events: list[dict]) -> dict:
    """사건 대표 기사의 저장된 E5 벡터를 한 번의 읽기 전용 SQL로 조회한다."""
    ids = sorted({e['representative']['id'] for e in events})
    if not ids:
        return {}
    with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                          autocommit=True, row_factory=dict_row, connect_timeout=10,
                          options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY; SET LOCAL statement_timeout='10s'")
            rows = conn.execute('SELECT article_id, embedding::text AS vector FROM article_embeddings '
                                'WHERE article_id=ANY(%s::bigint[])', (ids,)).fetchall()
    vectors = {r['article_id']: normalized_vector(r['vector']) for r in rows}
    return {(e.get('grouped', True), e['group_id']): vectors.get(e['representative']['id']) for e in events}


def filter_timeline_by_query(events: list[dict], vectors: dict, query_vector,
                             min_cos: float = TIMELINE_Q_MIN_COS, *, name_pattern: str = '') -> list[dict]:
    query = normalized_vector(query_vector)
    scored = []
    for index, event in enumerate(events):
        center = vectors.get((event.get('grouped', True), event['group_id']))
        score = sum(a * b for a, b in zip(center, query)) if center and query and len(center) == len(query) else None
        named = name_pattern and any(re.search(name_pattern, a.get('title', ''), re.IGNORECASE)
                                     for a in [event['representative'], *event.get('matches', [])])
        scored.append((index, event, score, min_cos - (TITLE_MATCH_COS_MARGIN if named else 0)))
    chosen = {i for i, event, score, threshold in scored
              if event.get('aspect_match') or any(match.get('title_core') for match in event.get('matches', ()))
              or (score is not None and score >= threshold)}
    return [event for i, event, _, _ in scored if i in chosen]


def timeline_centers(events: list[dict], params: dict | None = None) -> dict:
    """후보 사건의 전체 구성 기사 평균을 단일 SQL로 읽는다."""
    if not events:
        return {}
    groups = [e['group_id'] for e in events if e['grouped']]
    singles = [e['group_id'] for e in events if not e['grouped']]
    with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                          autocommit=True, row_factory=dict_row, connect_timeout=10,
                          options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout='10s'; SET LOCAL jit=off; SET LOCAL work_mem='32MB'")
            conditions, filter_values = timeline_article_filter(with_article_duplicates(conn, params or {}))
            rows = conn.execute(f"""
                SELECT members.grouped, members.group_id, avg(e.embedding)::text AS center
                FROM (
                    SELECT true AS grouped, wanted.group_id, g.article_id
                    FROM unnest(%s::bigint[]) wanted(group_id)
                    JOIN article_groups g ON g.group_id=wanted.group_id
                    UNION ALL
                    SELECT false, id, id FROM unnest(%s::bigint[]) wanted(id)
                ) members
                JOIN article_embeddings e ON e.article_id=members.article_id
                JOIN articles a ON a.id=members.article_id
                WHERE TRUE {conditions}
                GROUP BY members.grouped, members.group_id
            """, (groups, singles, *filter_values)).fetchall()
    return {(r['grouped'], r['group_id']): normalized_vector(r['center']) for r in rows}


def timeline_keywords(events: list[dict], required_keywords: list[str], params: dict | None = None) -> dict:
    """사건별 언급 합계 상위 10개에서 제외어를 뺀다(후순위 보충 없음). 단일 집계 SQL."""
    if not events:
        return {}
    groups = sorted({e['group_id'] for e in events if e['grouped']})
    singles = sorted({e['group_id'] for e in events if not e['grouped']})
    excluded = sorted(KEYWORD_STOPWORDS | MERGE_GENERAL_WORDS | set(required_keywords))
    with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                          autocommit=True, row_factory=dict_row, connect_timeout=10,
                          options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout='10s'; SET LOCAL jit=off; SET LOCAL work_mem='32MB'")
            conditions, filter_values = timeline_article_filter(with_article_duplicates(conn, params or {}))
            rows = conn.execute(f'''
                WITH members AS (
                    SELECT true AS grouped, wanted.group_id, g.article_id
                    FROM unnest(%s::bigint[]) wanted(group_id)
                    JOIN article_groups g ON g.group_id=wanted.group_id
                    UNION ALL
                    SELECT false, id, id FROM unnest(%s::bigint[]) wanted(id)
                ), sums AS (
                    SELECT m.grouped, m.group_id, k.keyword COLLATE "C" AS keyword,
                           sum(k.mention_count) AS mentions
                    FROM members m JOIN article_keywords k ON k.article_id=m.article_id
                    JOIN articles a ON a.id=m.article_id
                    WHERE TRUE {conditions}
                    GROUP BY m.grouped, m.group_id, k.keyword COLLATE "C"
                ), ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY grouped, group_id ORDER BY mentions DESC, keyword COLLATE "C"
                    ) AS rank FROM sums
                )
                SELECT grouped, group_id, keyword FROM ranked
                WHERE rank <= %s AND NOT (keyword=ANY(%s::text[]))
                ORDER BY grouped, group_id, rank
            ''', (groups, singles, *filter_values, MERGE_KEYWORD_LIMIT, excluded)).fetchall()
    features = {(e['grouped'], e['group_id']): set() for e in events}
    for row in rows:
        features[(row['grouped'], row['group_id'])].add(row['keyword'])
    return features


def event_coverage(event: dict) -> dict:
    """언론사 수, 보도 수, 제목 관련도 순서를 숫자 하나로 보존한다."""
    publisher_count = len(set(filter(None, event.get('publishers') or ())))
    score = event.get('score')
    relevance = (1 + max(-1.0, min(score, 1.0))) / 4 if score is not None else 0
    importance = publisher_count + 1 - 1 / (1 + event.get('article_count', 1) + relevance)
    return {'publisher_count': publisher_count, 'importance': importance}


def timeline_importance_rank(event: dict) -> tuple:
    return event.get('relevance', 0), event_coverage(event)['importance']


def merge_timeline_events(events: list[dict], centers: dict, keywords: dict, *, preserve_months: bool = False) -> list[dict]:
    """날짜순 이웃을 누적 병합한다. 벡터 누적합은 재정규화로 가중치가 변하지 않게 보존한다."""
    merged = []
    seen = set()
    for event in sorted(events, key=lambda e: (e['date_from'], e['date_to'], e['group_id'])):
        key = (event.get('grouped', True), event['group_id'])
        if key in seen:
            continue
        seen.add(key)
        center = centers.get(key)
        features = set(keywords.get(key, ()))
        weight = event['article_count']
        sub = {k: event[k] for k in ('group_id', 'date_from', 'date_to', 'representative', 'article_count')}
        sub['grouped'] = key[0]
        sub['summary'] = event.get('summary')
        sub.update(event_coverage(event))
        item = {**event, 'sub_events': [sub], 'event_count': 1, 'merge_checks': [],
                '_center': center, '_sum': [v * weight for v in center] if center else None,
                '_representative_count': weight, '_keywords': features}
        previous = merged[-1] if merged else None
        gap = ((date.fromisoformat(str(event['date_from'])) -
                date.fromisoformat(str(previous['date_to']))).days if previous else None)
        cosine = (sum(a * b for a, b in zip(previous['_center'], center))
                  if previous and previous['_center'] and center else None)
        shared = sorted(previous['_keywords'] & features) if previous else []
        span = ((date.fromisoformat(str(max(previous['date_to'], event['date_to']))) -
                 date.fromisoformat(str(previous['date_from']))).days if previous else None)
        if (previous and previous.get('subject_headlines') == event.get('subject_headlines')
                and (not preserve_months or str(previous['date_from'])[:7] == str(event['date_from'])[:7])
                and previous.get('category_mismatch') == event.get('category_mismatch')
                and bool(previous.get('aspect_match')) == bool(event.get('aspect_match'))
                and gap <= MERGE_GAP_DAYS and span <= MERGE_MAX_SPAN_DAYS
                and cosine is not None and cosine >= MERGE_MIN_COS and shared):
            previous['merge_checks'].append({'left_group_ids': [s['group_id'] for s in previous['sub_events']],
                                            'right_group_id': event['group_id'], 'gap_days': gap, 'cosine': cosine,
                                            'shared_keywords': shared, 'span_days': span})
            previous['_keywords'].update(features)
            previous['sub_events'].append(sub)
            previous['event_count'] += 1
            previous['article_count'] += weight
            previous['date_to'] = max(previous['date_to'], event['date_to'])
            previous['publishers'] = sorted(set(previous['publishers']) | set(event['publishers']))
            scores = (previous.get('score'), event.get('score'))
            previous['score'] = max((score for score in scores if score is not None), default=None)
            previous['matches'] = previous['matches'] + event['matches']
            if weight > previous['_representative_count']:
                previous['representative'] = event['representative']
                previous['_representative_count'] = weight
            previous['_sum'] = [a + b * weight for a, b in zip(previous['_sum'], center)]
            previous['_center'] = normalized_vector(previous['_sum'])
        else:
            merged.append(item)
    return [{**{k: v for k, v in item.items() if not k.startswith('_')}, **event_coverage(item)} for item in merged]


def select_timeline_events(result: dict, events: list[dict], *, limit: int = TIMELINE_LIMIT,
                           preserve_order: bool = False, month_limit: int | None = None) -> list[dict]:
    ranks = {}
    events_in_order = result['events'] or result.get('related_events', [])
    for index, event in enumerate(result.get('timeline_candidates', events_in_order)):
        key = (event.get('grouped', True), event['group_id'])
        ranks.setdefault(key, index)

    def rank(event):
        members = event.get('sub_events') or [event]
        keys = [(member.get('grouped', True), member['group_id']) for member in members]
        best_rank = min(ranks[key] for key in keys)
        return (event.get('subject_headlines', False), event.get('aspect_match', False),
                *timeline_importance_rank(event), -best_rank)

    ranked = sorted(events, key=rank, reverse=True)
    if month_limit is not None:
        counts, limited = {}, []
        for event in ranked:
            month = str(event['date_from'])[:7]
            count = counts.get(month, 0)
            if count < month_limit:
                limited.append(event)
                counts[month] = count + 1
        ranked = limited
    if result.get('monthly_event_selection'):
        limit = max(limit, len({str(event['date_from'])[:7] for event in ranked}))
    selected = ranked[:limit]
    if 1 < limit < len(ranked):
        month_indexes = {}
        for index, event in enumerate(ranked):
            month_indexes.setdefault(str(event['date_from'])[:7], index)
        representatives = [index for _, index in sorted(month_indexes.items())]
        if len(representatives) > limit:
            # 한도보다 사건이 있는 월이 많으면 첫 달과 마지막 달을 포함해 고르게 뽑는다.
            representatives = [representatives[i * (len(representatives) - 1) // (limit - 1)] for i in range(limit)]
        selected_indexes = set(representatives)
        for index in range(len(ranked)):
            if len(selected_indexes) == limit:
                break
            selected_indexes.add(index)
        selected = [ranked[index] for index in sorted(selected_indexes)]
    if preserve_order:
        selected_ids = {id(event) for event in selected}
        return [event for event in events if id(event) in selected_ids]
    return sorted(selected, key=lambda e: (e['date_from'], e['date_to'], e['group_id']))


def selected_timeline_events(result: dict) -> list[dict]:
    if 'timeline_events' in result:
        return result['timeline_events']
    return select_timeline_events(result, timeline_candidates(result))


def match_timeline(result: dict) -> dict:
    """항목 목록은 LLM 응답과 무관하게 서버 원본으로 구성한다."""
    selected = selected_timeline_events(result)
    items = []
    for index, event in enumerate(selected):
        publishers = event['publishers']
        primary = ('동아일보' if '동아일보' in publishers else
                   event['representative'].get('publisher') or next(iter(publishers), '언론사 미상'))
        item = {**event, **event_coverage(event), 'event_index': index, 'title': event['representative']['title'],
                'date_label': event['date_from'] if event['date_from'] == event['date_to'] else
                              f'{event["date_from"]} ~ {event["date_to"]}',
                'source_label': primary + (f' 외 {len(publishers)-1}개 언론사' if len(publishers) > 1 else ''),
                'publishers': publishers}
        items.append(item)
    return {'headline': selected[0]['representative']['title'] if selected else '',
            'items': items, 'model': llm_model()}


def timeline_summary_input(events: list[dict]) -> list[dict]:
    # 대표 제목과 중복되지 않는 하위 제목을 항목별 최대 6개 제공한다.
    return [{'event_index': i, 'number': f'[{i + 1}]', 'title': e['representative']['title'],
             'date_from': e['date_from'], 'date_to': e['date_to'], 'other_titles': list(dict.fromkeys(
                 s['representative']['title'] for s in e.get('sub_events', [])
                 if s['representative']['title'] != e['representative']['title']))[:6],
             **({'summary': '\n'.join(dict.fromkeys(s['summary'] for s in (e.get('sub_events') or [e])
                                                   if s.get('summary')))}
                if any(s.get('summary') for s in (e.get('sub_events') or [e])) else {})}
            for i, e in enumerate(events)]


def is_price_timeline(params: dict) -> bool:
    topic = params['q'] or ' '.join(params['keywords'])
    measures = (r'주가(?!\s*조작)|주식\s*시세|증시|코스피|코스닥|다우\s*지수|나스닥(?:\s*지수)?|'
                r'주가지수|환율|(?:기준|시장|대출|예금)?\s*금리|국채\s*수익률|'
                r'시세|금값|유가|집값|물가|가격(?!\s*담합)')
    return bool(re.search(rf'(?<!\w)(?:{measures})(?:{ALIAS_PARTICLE})?(?!\w)', topic, re.IGNORECASE))


def timeline_aspect_terms(params: dict) -> list[str]:
    """필수어와 그 별칭, 날짜, 일반어를 제외한 주제어를 질문 순서로 보존한다."""
    required = params.get('required_terms')
    topic = (' '.join(required) if required and params.get('supporting_terms')
             else QUESTION_DATE_CUE.sub(' ', params['q']))
    topic = normalize_query_term(topic, params.get('person_names', ()))
    names = set(params['keywords']) | set(query_entities(topic))
    aliases = {alias for name in names for alias in ENTITY_ALIASES.get(name, (name,))}
    if aliases:
        pattern = title_entity_pattern(sorted(aliases, key=lambda alias: (-len(alias), alias)), ignore_spacing=True)
        topic = re.sub(rf'(?<!\w)(?:{pattern})(?:{ALIAS_PARTICLE})?(?!\w)',
                       ' ', topic, flags=re.IGNORECASE)
    supporting = set(params.get('supporting_terms', ()))
    terms = list(dict.fromkeys(word for word in TOPIC_WORD.findall(topic)
                               if not word.isdecimal() and web_word_forms(word).isdisjoint(TIMELINE_ASPECT_FILLER)))
    terms = [word for word in terms if web_word_forms(word).isdisjoint(supporting)]
    terms.extend(word for word in params.get('required_terms', ())
                 if word in {'이적', '전당대회'} and word not in terms)
    if (params.get('target_category') and len(terms) == 1
            and re.fullmatch(r'[가-힣]{2,12}\s+[가-힣]{2,12}' + CONTEXT_SUFFIX, params['q'])
            and terms[0] not in {'이적', '전당대회'} and not topic_aspects(params['q'])):
        # 대분류 추정은 실제 직함에만 적용한다. 이적 등의 사건어는 관점으로 보존한다.
        return []
    return terms


def timeline_aspect_title_mismatch(title: str, summary: str, keywords: list[str], aspect_terms: list[str]) -> bool:
    """제목에서 사건의 관점이나 당사자가 어긋나는 명시적 단서를 확인한다."""
    if '전당대회' in aspect_terms:
        leadership = (r'전당\s*대회|당\s*대회|당권|(?:당\s*)?대표(?:직)?\s*(?:출마|선출|선거|경선|도전|후보)|'
                      r'(?:새|차기)\s*지도부|지도부\s*(?:선출|경선|경쟁)')
        if not re.search(leadership, title):
            return True
        parties = re.findall(rf'(?<!\w)({POLITICAL_PARTIES}|국힘)(?:의)?\s+(?:(?:새|차기|신임)\s+)?'
                             rf'(?:{leadership})', title)
        targets = {keyword for keyword in keywords if re.fullmatch(rf'{POLITICAL_PARTIES}|국힘', keyword)}
        if targets & {'국민의힘', '국힘'}:
            targets.update(('국민의힘', '국힘'))
        return bool(parties and targets and targets.isdisjoint(parties))
    if '이적' not in aspect_terms or len(keywords) != 1:
        return False
    transfer = r'이적|영입|입단|합류|계약|새\s*(?:유니폼|구단|팀)|팀을\s*옮기'
    current = re.sub(r'이적\s*(?:이후|후|뒤|이래|까지\s*했는데)', '', title)
    if not re.search(transfer, current):
        return True
    subject = rf'(?:{title_entity_pattern(ENTITY_ALIASES.get(keywords[0], keywords))})'
    if not re.search(subject, title) and not re.search(subject, summary):
        return True
    aside = (rf'{subject}\s*(?:(?:의\s*)?[\'"‘“]?(?:후계자|팀\s*동료|백업)|'
             r'(?:을|를|도)?\s*이어|(?:와|과)\s*달리)')
    if re.search(aside, title) and not re.search(subject, re.sub(aside, '', title)):
        return True
    rumor = re.search(r'이적설\s+([가-힣A-Za-z]{2,10})\s*[,，]', title)
    if rumor and not re.fullmatch(subject, rumor[1]) and not re.search(subject, title):
        return True
    fee = re.search(rf'{subject}.{{0,12}}(?:골|도움)\s+([가-힣]{{2,4}})\s+[\d,.]+\s*(?:억|만)(?:원)?\s+이적', title)
    if fee and not re.fullmatch(subject, fee[1]):
        return True
    clauses = re.split(r'\.{2,}|…+', title, maxsplit=1)
    if len(clauses) == 1:
        return False
    main, tail = clauses
    if re.search(rf'{subject}\s+떠난\s', main) and '이적설' in tail and not re.search(subject, tail):
        return True
    return bool(re.search(transfer, main) and not re.search(subject, main)
                and re.search(subject, tail) and re.search(r'뛴다|한솥밥|맞대결', tail) and not re.search(transfer, tail))


def judge_timeline_relevance(events: list[dict], params: dict, *, first_number: int = 1,
                             coverage: bool = False, batch_groups: list[tuple[int, bool]] | None = None,
                             compact: bool = False, deadline: float | None = None) -> tuple[list[dict], float]:
    numbers = [str(i) for i in range(first_number, first_number + len(events))]
    labels = (string.ascii_uppercase + string.ascii_lowercase)[:len(events)] if compact else numbers
    if len(labels) != len(events):
        raise LLMError('압축 관련성 판정의 기사 수를 초과했습니다.')
    schema = {'type': 'object', 'properties': {number: {'type': 'integer', 'enum': [0, 1, 2]} for number in numbers},
              'required': numbers, 'additionalProperties': False}
    evidence = timeline_summary_input(events)
    keywords = params['keywords']
    topic = params['q']
    if params.get('supporting_terms') and params.get('required_terms'):
        topic = ' '.join(params['required_terms'])
        supporting = set(params['supporting_terms'])
        keywords = [word for word in keywords if word not in supporting] or params['required_terms'][:1]
    aspect_terms = timeline_aspect_terms(params)
    if params.get('required_terms'):
        keywords = [word for word in keywords if word not in aspect_terms]
    named_topic = len(keywords) == 1 and not aspect_terms
    price = is_price_timeline(params)
    strict_title = not price and bool({'이적', '전당대회'}.intersection(aspect_terms))
    excluded = [strict_title and timeline_aspect_title_mismatch(item['title'], item.get('summary', ''), keywords, aspect_terms)
                for item in evidence]
    if strict_title:
        for item, omit in zip(evidence, excluded):
            if omit:
                item['title'] = '[제외] ' + item['title']
            if 'summary' in item:
                item['summary'] = ('' if omit else item['summary'][:160])
    if keywords:
        lines = ['검색 대상: ' + ', '.join(keywords), '질문: ' + topic, '번호별 기사 제목:']
        if aspect_terms:
            lines[1] += ' | 남은 주제어: ' + ' '.join(aspect_terms)
        evidence_lines = []
        for label, item in zip(labels, evidence):
            title = ' '.join(item['title'].splitlines())
            summary = ' '.join(item.get('summary', '').splitlines())
            evidence_lines.append(f'{label}. {title}' + (f' (요약: {summary})' if summary else ''))
        if named_topic and not price and compact and batch_groups:
            coverage_start = sum(n for n, extra in batch_groups if not extra)
            data = '\n'.join([lines[1], lines[0]] + [
                line + (' [보도량 후보]' if i >= coverage_start else '') for i, line in enumerate(evidence_lines)])
        elif batch_groups:
            start = 0
            for count, extra in batch_groups:
                lines.append('[보도량 후보]' if extra else '[유사도 후보]')
                lines.extend(evidence_lines[start:start + count])
                start += count
            data = '\n'.join(lines)
        else:
            data = '\n'.join(lines + evidence_lines)
    else:
        data = {'question': topic, 'aspect_terms': aspect_terms, 'events': [
            {**({'label': label} if compact else {'number': int(label)}), 'title': item['title'],
             **({'summary': item['summary']} if 'summary' in item else {})}
            for label, item in zip(labels, evidence)]}
    system = prompts.TIMELINE_ASPECT_RELEVANCE
    if price:
        if named_topic:
            system = prompts.KEYWORD_RELEVANCE_CRITERIA + prompts.NAMED_PRICE_RELEVANCE
        system += prompts.PRICE_RELEVANCE
    elif named_topic:
        system = prompts.NAMED_TOPIC_RELEVANCE
    elif '유출' in params['q']:
        system += prompts.NON_PRICE_RELEVANCE
    spacing_aliases = [f'{word} = {alias}' for word in keywords for alias in CORE_KEYWORD_ALIASES.get(word, ())
                       if ''.join(alias.split()) == ''.join(word.split())]
    if spacing_aliases:
        system += ' 다음 제목 표기는 띄어쓰기만 다른 동일한 검색 대상이다. ' + ', '.join(spacing_aliases) + '. '
    if strict_title:
        system += (prompts.TIMELINE_PARTY_LEADERSHIP_RELEVANCE if '전당대회' in aspect_terms
                   else prompts.TIMELINE_TRANSFER_RELEVANCE)
        system += ' 제목에 [제외]가 붙은 후보는 0이다. '
    if keywords and person_title_flag(params)[0]:
        system += ' 질문이 직함과 이름으로 사람을 지정하면 그 직함의 해당 인물이 주제인 보도를 2로 판정한다.'
    if (price or not named_topic) and (coverage or any(extra for _, extra in batch_groups or ())):
        system += prompts.TIMELINE_COVERAGE
    if not named_topic and '실적' in topic_aspects(params['q']):
        system += prompts.TIMELINE_EARNINGS_RELEVANCE
    if compact:
        system = system.replace('번호별', '기호별') + prompts.COMPACT_RELEVANCE_OUTPUT
        pattern = ','.join(f'{label}[012]' for label in labels)
        schema = {'type': 'object', 'properties': {'r': {'type': 'string', 'pattern': f'^{pattern}$'}},
                  'required': ['r'], 'additionalProperties': False}
        options = {} if llm_think() else {'num_predict': 16 + 4 * len(events)}
        raw, elapsed = ollama_chat(system, data, schema, deadline, **options)
        packed = raw.get('r')
        if not isinstance(packed, str) or not re.fullmatch(pattern, packed) or set(raw) != {'r'}:
            raise LLMError('사건 관련성 판정 응답 형식이 잘못되었습니다.')
        grades = [int(pair[-1]) for pair in packed.split(',')]
        if (not aspect_terms and len(grades) >= 9
                and (not any(grades) or not named_topic and sum(bool(g) for g in grades) >= len(grades) * .9)):
            raise LLMError('압축 관련성 판정에서 대부분의 후보에 같은 채택 여부가 반환되었습니다.')
    else:
        system += '번호마다 0, 1, 2 중 하나를 쓴다.'
        decisions, elapsed = ollama_chat(system, data, schema, deadline)
        if any(type(decisions.get(number)) is not int or decisions[number] not in (0, 1, 2) for number in numbers):
            raise LLMError('사건 관련성 판정 응답 형식이 잘못되었습니다.')
        grades = [decisions[number] for number in numbers]
    return [{**event, 'relevance': grade} for event, grade, omit in zip(events, grades, excluded)
            if grade and not omit], elapsed


def timeline_month_input(events: list[dict]) -> list[dict]:
    """기존 흐름과 같이 사건 시작 월로 묶고 보도량·대표/하위 제목을 제공한다."""
    months = {}
    events = sorted(events, key=lambda event: (event['date_from'], event['date_to']))
    for event, item in zip(events, timeline_summary_input(events)):
        month = str(event['date_from'])[:7]
        group = months.setdefault(month, {'month': month, 'article_count': 0, 'event_count': 0, 'events': []})
        group['article_count'] += event.get('article_count', 1)
        group['event_count'] += event.get('event_count', 1)
        group['events'].append({**{k: item[k] for k in ('number', 'date_from', 'date_to', 'title', 'other_titles', 'summary') if k in item},
                                'article_count': event.get('article_count', 1),
                                'publishers': event.get('publishers', []), 'score': event.get('score'),
                                'relevance': event.get('relevance', 0),
                                **({'aspect_subject_match': True} if event.get('aspect_subject_match') else {})})
    return [months[month] for month in sorted(months)]


def timeline_month_representative(events) -> dict:
    return max(events, key=lambda e: (e.get('aspect_subject_match', False), *timeline_importance_rank(e)))


def can_summarize_month(month: dict) -> bool:
    return len(month['events']) >= 3 and len({event['date_from'] for event in month['events']}) >= 2


def clean_month_title(title: str) -> str:
    """키워드는 보존하고 표식·따옴표·말줄임표만 제거한다."""
    clean = re.sub(r'\[\s*(?:속보|단독)\s*\]|["\'“”‘’「」『』…]|\.{3,}', '', title)
    clean = ' '.join(clean.split()).strip(' ·,;:-')
    if len(clean) > 45:
        prefix = clean[:45]
        # 공백 없는 긴 단어는 다른 단어 경계가 없으므로 길이 상한에서 자른다.
        clean = prefix.rsplit(' ', 1)[0] if ' ' in prefix and not clean[45].isspace() else prefix
    return clean.rstrip(' ·,;:-') or '제목 없음'


def month_number_tokens(text: str) -> set[str]:
    text = unicodedata.normalize('NFKC', text)
    return summary_number_tokens(text) | {str(int(m)) + '월' for m in re.findall(r'(?<!\d)(\d+)\s*월', text)}


def month_title_clause(title: str, subject: str) -> str:
    if not subject:
        return title
    match = re.search(subject, title, re.IGNORECASE)
    if not match:
        return ''
    # 이름 바로 뒤의 쉼표는 제목 구분에 쓰이기도 한다. 뒷 절의 다른 종목은 제외한다.
    return re.split(r'(?<!\d),|,(?!\d)|[，;·]|\s+(?:속|가운데)\b',
                    title[match.end():].lstrip(' ,，·'), maxsplit=1)[0]


def month_directions(text: str) -> list[str]:
    return [MONTH_DIRECTION[match.group()] for match in MONTH_DIRECTION_PATTERN.finditer(text)]


def month_title_excerpt(title: str, subject: str) -> str:
    clause = month_title_clause(title, subject)
    market = MONTH_MARKET_TITLE.match(title) if subject else None
    if market:
        clause = title[market.end():]
    move = MONTH_DIRECTION_PATTERN.search(clause)
    price = re.search(r'주가(?!조작)', clause) if subject and not market else None
    if price:
        move = MONTH_DIRECTION_PATTERN.search(clause, price.end()) or move
    if not move or MONTH_UNCERTAINTY.search(clause):
        return ''
    value = MONTH_PRICE_MOVE.search(clause[:move.start()])
    direction = move.group() if move.group() == '폭락' else MONTH_DIRECTION[move.group()]
    text = f'{value.group().strip()} {direction}' if value else direction
    if market:
        halt = MONTH_TRADING_HALT.search(clause)
        text = f'{market[1].replace("-", "·")} {text}' + (f'·{halt.group()}' if halt else '')
    elif subject and (earnings := MONTH_EARNINGS.search(clause[:move.start()])):
        text = f'{earnings.group()} {"주가 " if price else ""}{text}'
    return text


def select_month_flow_events(events: list[dict], subject: str = '') -> list[dict]:
    """시기마다 보도량이 가장 큰 사건을 고르고, 시기가 적으면 다른 날짜를 보충한다."""
    by_day = {}
    for event in sorted(events, key=lambda e: (*(-value for value in timeline_importance_rank(e)),
                                                not e.get('aspect_subject_match'), e['date_from'])):
        by_day.setdefault(event['date_from'], event)
    by_phase = {}
    for event in by_day.values():
        by_phase.setdefault(month_phase(event['date_from']), event)
    if len(by_phase) > 1:
        for phase in (min(by_phase), max(by_phase)):
            direct_events = [event for event in (events if subject else by_day.values()) if event.get('aspect_subject_match')
                             and month_phase(event['date_from']) == phase]
            direct = max(direct_events, key=lambda e: (bool(subject and month_title_excerpt(e['title'], subject)),
                                                      *timeline_importance_rank(e), -int(e['date_from'][-2:])), default=None)
            if direct:
                by_phase[phase] = direct
    picked = {event['date_from']: event for event in by_phase.values()}
    for day, event in by_day.items():
        if len(picked) == 3:
            break
        picked.setdefault(day, event)
    return [picked[day] for day in sorted(picked)]


def month_flow_date(event: dict) -> str:
    start, end = event['date_from'], event['date_to']
    label = f'{int(start[5:7])}/{int(start[8:10])}'
    if start == end:
        return label
    end_label = f'{int(end[8:10])}' if end[:7] == start[:7] else f'{int(end[5:7])}/{int(end[8:10])}'
    return f'{label}~{end_label}'


def month_coverage_titles(month: dict) -> list[dict]:
    by_day = {}
    for event in sorted(month['events'], key=timeline_importance_rank, reverse=True):
        by_day.setdefault(event['date_from'], event)
    return sorted(list(by_day.values())[:3], key=lambda event: event['date_from'])


def rule_month_flow(month: dict, subject: str = '', *, price: bool = False) -> str:
    if not (price or subject):
        events = month_coverage_titles(month)
        return (' → '.join(f'{month_flow_date(event)} {clean_month_title(event["title"])}' for event in events)
                if len(events) > 1 else '')
    candidates = []
    for event in month['events']:
        for title in (event['title'], *event['other_titles']):
            if excerpt := month_title_excerpt(title, subject):
                candidates.append({**event, 'excerpt': excerpt})
                break
    return ' → '.join(f'{month_flow_date(event)} {event["excerpt"]}'
                      for event in select_month_flow_events(candidates, subject))


def validate_month_text(text, month: dict, subject: str = '') -> list[str]:
    """월 흐름의 한 단계에 대해 제목의 수치·방향과 제공된 인용 근거를 검사한다."""
    if not isinstance(text, str) or not 2 <= len(text.strip()) <= 32 or '\n' in text or '\r' in text or '→' in text:
        return ['text 문자열 형식 위반']
    titles = [t for event in month['events'] for t in [event['title'], *event['other_titles']]]
    reasons = []
    evidence = titles + [e['summary'] for e in month['events'] if e.get('summary')]
    if subject:
        mentions_subject = re.search(subject, text, re.IGNORECASE)
        clauses = [clause for title in titles if (clause := month_title_clause(title, subject))]
        markets = set(MONTH_MARKET.findall(text))
        if markets - {market for title in titles for market in MONTH_MARKET.findall(title)}:
            reasons.append('근거 제목에 없는 시장')
        if mentions_subject or (clauses and not MONTH_MARKET.search(text)):
            titles = clauses
    allowed = set().union(*(month_number_tokens(t) for t in titles))
    invalid = sorted(month_number_tokens(text) - allowed)
    if invalid:
        reasons.append('근거 제목에 없는 숫자·단위: ' + ', '.join(invalid))
    directions = set(month_directions(text))
    if directions - {cue for title in titles for cue in month_directions(title)}:
        reasons.append('근거 제목에 없는 방향')
    patterns = (r'"([^"\n]*)"', r"'([^'\n]*)'", r'“([^”\n]*)”', r'‘([^’\n]*)’',
                r'「([^」\n]*)」', r'『([^』\n]*)』')
    remaining, invalid_quote = text, False
    for pattern in patterns:
        for quote in re.findall(pattern, text):
            if not quote or not any(quote in source for source in evidence):
                invalid_quote = True
        remaining = re.sub(pattern, '', remaining)
    if invalid_quote or any(mark in remaining for mark in '"\'“”‘’「」『』'):
        reasons.append('그 달 근거에 없는 인용구')
    if any(int(m) != int(month['month'][5:]) for m in re.findall(r'(?<!\d)(\d+)\s*월', text)):
        reasons.append('다른 달 언급')
    return reasons


def month_phase(day: str) -> int:
    return min((int(day[8:10]) - 1) // 10, 2)


def match_month_steps(steps, month: dict, *, preserve_uncertainty: bool, subject: str) -> tuple[str, list[str]]:
    events = select_month_flow_events(month['events'], subject)
    if not isinstance(steps, list) or len(steps) != len(events):
        return '', ['월 흐름 응답 형식 위반']
    segments = []
    step_phases = [month_phase(event['date_from']) for event in events]
    month_number = int(month['month'][5:])
    for event, text, phase in zip(events, steps, step_phases):
        reasons = validate_month_text(text, {**month, 'events': [event]}, subject)
        if reasons:
            return '', reasons
        if subject and (excerpt := month_title_excerpt(event['title'], subject)):
            if not month_directions(text):
                return '', ['근거 제목의 방향 누락']
            if '주가' in excerpt and re.search(r'영업(?:이익|익)', text) and '주가' not in text:
                return '', ['실적과 주가의 구분 누락']
        if (preserve_uncertainty and not MONTH_UNCERTAINTY.search(text)
                and not event.get('summary') and MONTH_UNCERTAINTY.search(event['title'])):
            return '', ['근거 제목의 불확실한 표현 누락']
        text = text.strip()
        if subject and not MONTH_MARKET.search(text) and (market := MONTH_MARKET_TITLE.match(event['title'])):
            text = f'{market[1]} {text}'
        if step_phases.count(phase) == 1:
            period = ('초', '중순', '말')[phase]
            label = period if segments else f'{month_number}월 {period}'
        else:
            label = month_flow_date(event)
        segments.append(f'{label} {text}')
    return ' → '.join(segments), []


def match_timeline_months(raw: dict, months: list[dict], *, preserve_uncertainty: bool = False,
                          aspect: str = '', aspect_subject: str = '', price: bool = False) -> dict:
    responses, sources, duplicates = {}, {}, set()
    for entry in raw.get('months', []) if isinstance(raw.get('months'), list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get('month'), str):
            continue
        key = entry['month']
        if key in responses:
            duplicates.add(key)
        responses[key] = entry.get('steps')
        sources[key] = entry.get('source', 'AI')
    matched = []
    for month in months:
        key = month['month']
        text, reasons = '', []
        if (price or aspect_subject or key in responses) and can_summarize_month(month):
            if key in duplicates:
                reasons = ['월 응답 중복']
            else:
                text, reasons = match_month_steps(responses.get(key), month,
                                                  preserve_uncertainty=preserve_uncertainty, subject=aspect_subject)
        representative = timeline_month_representative(month['events'])
        title = representative['title']
        if (aspect and text and not reasons and re.search(aspect, title, re.IGNORECASE)
                and not re.search(aspect, text, re.IGNORECASE) and not month_directions(text)):
            reasons.append('질문 측면 누락')
        source = sources.get(key, 'AI')
        if not text or reasons:
            text = rule_month_flow(month, aspect_subject, price=price)
            source = '규칙' if text else '제목'
        matched.append({'month': key, 'text': text or clean_month_title(title),
                        'source': source, 'checks': reasons,
                        'article_count': month['article_count'], 'event_count': month['event_count']})
    return {'months': matched, 'month_fallback_count': sum(m['source'] != 'AI' for m in matched)}


def summary_number_tokens(text: str) -> set[str]:
    # 날짜의 월만 예외다. '6개월'·'2년'·'1심' 등 단위는 숫자와 함께 대조한다.
    text = re.sub(r'(?<!\d)\d+\s*월', '', text)
    tokens = re.findall(r'\d+(?:[.,]\d+)*(?:\s*여)?(?:\s*(?:조|억|만|천))?'
                        r'(?:\s*(?:퍼센트포인트|퍼센트|개월|달러|차례|시간|마리|%p|kg|km|년|일|명|건|회|차|배|원|%|심|도|개|대|번|분|초|점|석|표|톤|m))?', text)
    return {re.sub(r'[\s,]', '', token) for token in tokens}


def timeline_stages(text: str) -> list[str]:
    """공백을 제거한 문자열에서 겹치지 않는 단계 어휘를 등장 순서대로 찾는다."""
    return [TIMELINE_STAGE_BY_ALIAS[match.group()] for match in
            TIMELINE_STAGE_PATTERN.finditer(re.sub(r'\s+', '', text))]


def validate_summary_stage_order(summary: str, items: list[dict]) -> list[str]:
    """꼬리표·대표/전체 하위 제목에 나온 단계의 최초 항목 인덱스와 대조한다."""
    first = {}
    for index, item in enumerate(sorted(items, key=lambda e: (e['date_from'], e['date_to']))):
        texts = [item.get('label', ''), item.get('summary') or '',
                 item.get('title') or item.get('representative', {}).get('title', ''),
                 *item.get('other_titles', []),
                 *(child.get('summary') or '' for child in item.get('sub_events', [])),
                 *(child['representative']['title'] for child in item.get('sub_events', []))]
        for text in texts:
            for stage in timeline_stages(text):
                first.setdefault(stage, index)
    stages = timeline_stages(summary)
    missing = sorted(set(stages) - first.keys())
    reasons = ['타임라인에 없는 사건 단계: ' + ', '.join(missing)] if missing else []
    indices = [first[stage] for stage in stages if stage in first]
    if any(before > after for before, after in zip(indices, indices[1:])):
        reasons.append('사건 단계 순서 역전')
    return reasons


def validate_timeline_summary(summary, inputs: list[dict], required_keywords: list[str],
                              timeline_items: list[dict] | None = None) -> list[str]:
    if not isinstance(summary, str):
        return ['summary 문자열 형식 위반']
    evidence = [t for item in inputs for t in [item['title'], *item['other_titles'], item.get('summary') or '']]
    allowed_numbers = set().union(*(summary_number_tokens(t) for t in evidence))
    dates = {date.fromisoformat(item[key]) for item in inputs for key in ('date_from', 'date_to')}
    for day in dates:
        allowed_numbers.update((f'{day.year}년', f'{day.month}월', f'{day.day}일',
                                str(day.year), str(day.month), str(day.day)))
    reasons = []
    invalid_numbers = sorted(summary_number_tokens(summary) - allowed_numbers)
    if invalid_numbers:
        reasons.append('입력 근거에 없는 숫자·단위: ' + ', '.join(invalid_numbers))
    quote_patterns = (r'"([^"\n]*)"', r"'([^'\n]*)'", r'“([^”\n]*)”', r'‘([^’\n]*)’',
                      r'「([^」\n]*)」', r'『([^』\n]*)』')
    quoted = any(not quote or not any(quote in source for source in evidence)
                 for pattern in quote_patterns for quote in re.findall(pattern, summary))
    remaining = summary
    for pattern in quote_patterns:
        remaining = re.sub(pattern, '', remaining)
    if quoted or any(mark in remaining for mark in '\"\'“”‘’「」『』'):
        reasons.append('입력 근거에 없는 인용구')
    if required_keywords and not any(keyword in summary for keyword in required_keywords):
        reasons.append('필수 키워드 최소 1개 누락')
    months = set()
    for item in inputs:
        start, end = date.fromisoformat(item['date_from']), date.fromisoformat(item['date_to'])
        months.update(index % 12 + 1 for index in range(start.year * 12 + start.month - 1,
                                                       end.year * 12 + end.month))
    if {int(m) for m in re.findall(r'(?<!\d)(\d+)\s*월', summary)} - months:
        reasons.append('항목 날짜 범위에 없는 월')
    if any((int(m), int(d)) not in {(day.month, day.day) for day in dates}
           for m, d in re.findall(r'(?<!\d)(\d+)\s*월\s*(\d+)\s*일', summary)):
        reasons.append('항목 날짜에 없는 월일')
    if not 40 <= len(summary) <= 250:
        reasons.append('길이 40~250자 위반')
    reasons.extend(validate_summary_stage_order(summary, inputs if timeline_items is None else timeline_items))
    return reasons


def timeline_context_words(text: str) -> set[str]:
    text = MONTH_DIRECTION_PATTERN.sub(lambda match: MONTH_DIRECTION[match.group()], text.casefold())
    return set(re.findall(r'\w+', text.replace('영업이익', '영업익')))


def match_timeline_summary(raw: dict, inputs: list[dict], required_keywords: list[str],
                           timeline_items: list[dict] | None = None, *,
                           verified_months: list[dict] | None = None) -> dict:
    """월 흐름 밖의 기사 근거가 확인되는 리드만 표시한다."""
    summary = raw.get('summary')
    reasons = validate_timeline_summary(summary, inputs, required_keywords, timeline_items)
    if not reasons and verified_months:
        evidence = [source for item in inputs for source in (item['title'], *item['other_titles'], item.get('summary', ''))]
        numbers_by_source = [summary_number_tokens(source) for source in evidence]
        sentences = re.split(r'(?<=[.!?])\s+', summary)
        if any(not any(numbers <= allowed for allowed in numbers_by_source)
               for numbers in map(summary_number_tokens, sentences)):
            reasons.append('다른 사건의 수치 결합')
        context = raw.get('context', '')
        flow_words = timeline_context_words(' '.join(month['text'] for month in verified_months))
        context_words = timeline_context_words(context) if isinstance(context, str) else set()
        if (len(context_words) < 2 or '→' in summary or context not in summary
                or not any(context in source for source in evidence)
                or not any(len(word) >= 2 and not any(old in word or word in old for old in flow_words if len(old) >= 2)
                           for word in context_words)):
            reasons.append('월 흐름에 없는 기사 근거 누락')
    return {'lead': None if reasons else summary, 'summary_status': '생략' if reasons else '통과',
            'summary_attempts': 1, 'summary_checks': [{'attempt': 1, 'reasons': reasons}]}


def summarize_timeline(month_inputs: list[dict], params: dict, deadline: float | None = None, *,
                       aspect_subject: str = '') -> tuple[dict, float]:
    flow, headlines, title_flows, displayed_flow = {}, [], [], []
    price = is_price_timeline(params)
    for month in month_inputs:
        selected = select_month_flow_events(month['events'], aspect_subject) if price and can_summarize_month(month) else []
        steps = []
        complete_price_flow = bool(selected and aspect_subject)
        for event in selected:
            title = event['title']
            name = re.search(aspect_subject, title, re.IGNORECASE) if aspect_subject else None
            if name and (clause := month_title_clause(title, aspect_subject)) and month_directions(clause):
                title = f'{name.group()} {clause}'
            excerpt = month_title_excerpt(event['title'], aspect_subject) if aspect_subject else ''
            if excerpt:
                title = f'{name.group()} {excerpt}' if name and not MONTH_MARKET_TITLE.match(event['title']) else excerpt
            else:
                complete_price_flow = False
            steps.append(title)
        if complete_price_flow:
            title_flows.append({'month': month['month'], 'steps': steps, 'source': '규칙'})
        elif steps:
            flow[month['month']] = steps
        displayed_flow.extend(steps or [clean_month_title(e['title']) for e in month_coverage_titles(month)])
        for event in month['events']:
            title = event['title']
            name = re.search(aspect_subject, title, re.IGNORECASE) if aspect_subject else None
            # 한 제목에 이어진 다른 종목의 등락 수치를 리드 입력에서 제외한다.
            if name and (clause := month_title_clause(title, aspect_subject)) and month_directions(clause):
                title = f'{name.group()} {clause}'
            headlines.append(f'{title} — {event["summary"]}' if event.get('summary') else title)
    # 기사 번호와 월 표시는 서버에서 관리한다. 모델에는 날짜순 제목 배열만 전달한다.
    groups = dict(zip(string.ascii_uppercase, flow.values()))
    properties = {'context': {'type': 'string', 'maxLength': 40}, 'lead': {'type': 'string', 'maxLength': 96}}
    if flow:
        properties['flows'] = {
            'type': 'object', 'required': list(groups), 'additionalProperties': False,
            'properties': {key: {'type': 'array', 'minItems': len(steps), 'maxItems': len(steps),
                                 'items': {'type': 'string', 'minLength': 2, 'maxLength': 32}}
                           for key, steps in groups.items()},
        }
    schema = {'type': 'object', 'properties': properties,
              'required': list(properties), 'additionalProperties': False}
    system = prompts.TIMELINE_SUMMARY
    if flow:
        system += prompts.TIMELINE_MONTH_FLOW
    data = {}
    if params['q']:
        system += prompts.TIMELINE_QUERY_SUMMARY
        data['question'] = params['q']
    data.update(required_keywords=params['keywords'], events=headlines, displayed_flow=displayed_flow)
    if flow:
        data['flow'] = groups
    # 생각 모드에서는 같은 토큰 상한이 생각 텍스트에도 적용된다.
    options = {} if llm_think() else {'num_predict': 136 + 36 * sum(map(len, flow.values())) + 12 * len(flow)}
    raw, elapsed = ollama_chat(system, data, schema, deadline, **options)
    flow_rows = raw.get('flows')
    months = ([{'month': month, 'steps': flow_rows.get(key)} for month, key in zip(flow, groups)]
              if isinstance(flow_rows, dict) else [])
    return {'summary': raw.get('lead'), 'context': raw.get('context'), 'months': [*title_flows, *months]}, elapsed


def expand_timeline_months(approved: list[dict], month_candidates: dict[str, list[dict]],
                           rejected: list[dict]) -> tuple[list[dict], list[dict]]:
    def keys(events):
        return {(sub['grouped'], sub['group_id']) for event in events for sub in event['sub_events']}

    used = keys(approved)
    excluded = used | keys(rejected)
    summaries = list(approved)
    display = list(approved)
    counts = {}
    for event in approved:
        month = str(event['date_from'])[:7]
        counts[month] = counts.get(month, 0) + 1
    for month, candidates in month_candidates.items():
        count = counts.get(month, 0)
        available = [event for event in candidates if (event['grouped'], event['group_id']) not in excluded]
        # 판정에서 빠진 달도 차트에 보도가 있으면 대표 제목 한 건을 남긴다.
        if not count and not available:
            available = [next((event for event in candidates if (event['grouped'], event['group_id']) not in used),
                              candidates[0])]
        for candidate in available[:TIMELINE_MONTH_LIMIT - count]:
            event, = merge_timeline_events([candidate], {}, {})
            event['_monthly_article_ids'] = candidate['_monthly_article_ids']
            event['_timeline_additional'] = bool(count)
            if not count:
                summaries.append(event)
            display.append(event)
            count += 1
    return sorted(display, key=lambda event: (event['date_from'], event['group_id'])), summaries


def generate_timeline(result: dict) -> dict:
    started = perf_counter()
    params = timeline_params(result)
    digest = params.get('mode') == 'digest'
    has_query = bool(params['q']) and not digest
    price = is_price_timeline(params)
    signals = title_signals(params) if has_query else None
    filtered_params = (params,) if timeline_article_filter(params, dedupe=False)[0] else ()
    tiered = 'related_events' in result or any('tier' in event for event in result['events'])
    cover_months = 'timeline_month_candidates' in result
    month_candidates = result.get('timeline_month_candidates', {})
    if (not (result['events'] or result.get('related_events') or month_candidates)
            and (tiered or not params['keywords'] or result.get('data_absence'))):
        return {"items": [], "headline": "", "lead": "", "model": llm_model(), "llm_ms": 0, "cached": False,
                "filter_relaxed": False, "no_relevant_events": has_query}
    candidates = (keyword_timeline_candidates(params) if params['keywords'] and not tiered
                  else timeline_candidates(result))
    preserve_months = cover_months or bool(result.get('monthly_event_selection'))
    if cover_months:
        # 차트에 보도가 있는 월을 기준으로 판단하고 월별 후보는 최종 빈자리에 사용한다.
        candidates = [event for event in candidates if str(event['date_from'])[:7] in month_candidates]
    if params['keywords'] == ['디어워즈']:
        # '스토브인디 어워즈'처럼 다른 시상식 이름에 붙어 있는 경우는 제외한다.
        embedded_name = re.compile(r'\B디\s*어워즈')
        standalone_name = re.compile(title_entity_pattern(params['keywords'], ignore_spacing=True))
        candidates = [event for event in candidates if not embedded_name.search(event['representative']['title'])
                      or standalone_name.search(event['representative']['title'])]
    if timeline_context_filter(params)[0] or params.get('target_category'):
        # 저장된 묶음 요약은 현재 맥락에서 제외된 기사를 인용할 수 있다.
        candidates = [{**event, 'summary': None} for event in candidates]
    retrieved_count = len(candidates)
    query_min_cos = timeline_query_min_cos() if params['keywords'] and params['q'] else 0.85
    candidate_ms = (perf_counter() - started) * 1000
    key = (TIMELINE_REVISION, os.environ.get("OLLAMA_URL", ""), llm_model(), llm_think(),
           tuple(sorted(params["keywords"])), params["q"], params["limit"],
           tuple(params.get('required_terms', ())), tuple(params.get('supporting_terms', ())),
           params.get('date_from'), params.get('date_to'), params.get('publisher'),
           tuple(params.get('excluded_categories', ())), params.get('target_category'),
           MERGE_GAP_DAYS, MERGE_MIN_COS, MERGE_MAX_SPAN_DAYS, MERGE_KEYWORD_LIMIT, TIMELINE_LIMIT, TIMELINE_CANDIDATES,
           query_min_cos, preserve_months,
           json.dumps([candidates, month_candidates], ensure_ascii=False, sort_keys=True))
    cached = _cache_get(_timeline_cache, key)
    if cached is not None:
        return {**cached, "cached": True, 'generation_ms': (perf_counter() - started) * 1000}
    retrieved = candidates
    coverage_keys = {(e.get('grouped', True), e['group_id']) for e in retrieved
                     if e.get('coverage_rank', math.inf) <= TIMELINE_COVERAGE_CANDIDATES}
    candidates = [e for e in retrieved if e.get('similarity_rank', 0) <= KEYWORD_TIMELINE_CANDIDATES]
    centers = {}
    filter_relaxed = bool(has_query and params['keywords'] and not tiered and not candidates)
    filtered_count = len(candidates)
    core_with_support = bool(params.get('supporting_terms') and all(e.get('tier') == 'core' for e in candidates))
    if has_query and candidates and not core_with_support:
        vectors = (timeline_representative_vectors(candidates) if params['keywords']
                   else timeline_centers(candidates, *filtered_params))
        if not params['keywords']:
            centers = vectors
        filtered = filter_timeline_by_query(candidates, vectors, query_embedding(params['q']), query_min_cos,
                                            **({'name_pattern': signals[0]} if signals else {}))
        filtered_count = len(filtered)
        filter_relaxed = bool(params['keywords'] and filtered_count < 3
                              and (not tiered or filtered_count < len(candidates)))
        if not filter_relaxed:
            candidates = filtered
    if not candidates and not month_candidates:
        timeline = {'items': [], 'headline': '', 'lead': '', 'model': llm_model(),
                    'llm_ms': 0, 'llm_calls': 0, 'cached': False, 'months': [], 'month_fallback_count': 0,
                    'summary_status': '생략', 'summary_attempts': 0, 'summary_checks': [],
                    'candidate_count': retrieved_count, 'filtered_candidate_count': 0, 'merged_count': 0,
                    'query_min_cos': query_min_cos, 'filter_relaxed': filter_relaxed, 'no_relevant_events': has_query,
                    'generation_ms': (perf_counter() - started) * 1000}
        _cache_put(_timeline_cache, key, timeline)
        return timeline
    if params['keywords'] or not has_query or core_with_support:
        centers = timeline_centers(candidates, *filtered_params)
    merged = merge_timeline_events(candidates, centers, timeline_keywords(candidates, params['keywords'], *filtered_params),
                                   preserve_months=preserve_months)
    merge_ms = (perf_counter() - started) * 1000
    # 계층이 있는 검색은 원래 검색 순위를 동률 기준으로 유지한다.
    result = {**result, **({'timeline_candidates': retrieved} if params['keywords'] and not tiered else {})}
    if cover_months:
        # 관련성 판정과 AI 요약의 입력 한도를 그대로 유지한다.
        result.pop('monthly_event_selection', None)
    relevance_status, relevance_ms, relevance_calls, excluded = '미실행', 0, 0, 0
    rejected = []
    eligible = merged
    deadline = monotonic() + LLM_TIMEOUT
    if has_query and merged:
        shortlist = select_timeline_events(result, merged, limit=TIMELINE_CANDIDATES)
        coverage_shortlist = []
        if coverage_keys:
            # 보도량이 큰 사건을 추가 판정하되 판정 실패 시에는 기존 유사도 후보만 사용한다.
            retained_keys = {(e.get('grouped', True), e['group_id']) for e in candidates}
            extra_keys = coverage_keys - retained_keys
            extra = [e for e in retrieved if (e.get('grouped', True), e['group_id']) in extra_keys]
            coverage_shortlist = [e for e in merged + merge_timeline_events(extra, {}, {}) if e not in shortlist
                                  and any((s['grouped'], s['group_id']) in coverage_keys for s in e['sub_events'])]
        relevance_started = perf_counter()
        pool = shortlist + coverage_shortlist
        batches = []
        for batch_candidates, coverage in ((shortlist, False), (coverage_shortlist, True)):
            size = len(batch_candidates)
            count = math.ceil(size / TIMELINE_RELEVANCE_BATCH)
            batches.extend(((i + 1) * size // count - i * size // count, coverage) for i in range(count))
        relevance_status = '판정 실패'
        if len(batches) > 1:
            attempts = [
                {'batch_groups': batches, 'compact': True, 'deadline': min(deadline, monotonic() + LLM_TIMEOUT / 3)},
                {'batch_groups': batches, 'deadline': deadline},
            ]
        else:
            attempts = [{'deadline': deadline}]
        for options in attempts:
            relevance_calls += 1
            try:
                eligible, elapsed = judge_timeline_relevance(pool, params, **options)
            except LLMError:
                relevance_ms = (perf_counter() - relevance_started) * 1000
            else:
                relevance_ms += elapsed
                relevance_status, excluded = '판정 완료', len(pool) - len(eligible)
                if cover_months:
                    retained = {(event['grouped'], event['group_id']) for event in eligible}
                    rejected = [event for event in pool if (event['grouped'], event['group_id']) not in retained]
                break
        if relevance_status == '판정 실패' and timeline_aspect_terms(params):
            # 주제어가 남는 질의에는 관련성 판정을 받지 않은 사건을 표시하지 않는다.
            eligible = []
    aspect = topic_aspect_pattern(params['q']) if price else ''
    aspect_subject = signals[0] if aspect and signals else ''
    if aspect_subject and '삼성전자' in params['keywords']:
        # 이 표기들은 주가 제목의 근거 확인에만 사용한다.
        aspect_subject += r'|(?<!\w)삼전닉스|(?<!\w)삼성(?=\s*[,，]\s*반도체)'
    if aspect_subject:
        eligible = [{**e, 'aspect_subject_match': bool(re.search(
            aspect_subject, e['representative']['title'], re.IGNORECASE))} if e.get('aspect_match') else e
                    for e in eligible]
    month_limit = TIMELINE_MONTH_LIMIT if cover_months else None
    summary_events = select_timeline_events(result, eligible, month_limit=month_limit)
    inputs = timeline_summary_input(summary_events)
    month_inputs = timeline_month_input(summary_events)
    raw = None
    llm_calls, llm_ms = relevance_calls, relevance_ms
    if month_inputs and relevance_status != '판정 실패' and llm_calls < 2:
        summary_started = perf_counter()
        llm_calls += 1
        try:
            raw, elapsed = summarize_timeline(month_inputs, params, deadline, aspect_subject=aspect_subject)
            llm_ms += elapsed
        except LLMError:
            llm_ms += (perf_counter() - summary_started) * 1000
    result['timeline_events'] = summary_events
    if cover_months:
        approved = select_timeline_events({**result, 'monthly_event_selection': True}, eligible, month_limit=month_limit)
        result['timeline_events'], month_representatives = expand_timeline_months(approved, month_candidates, rejected)
        month_inputs = timeline_month_input(month_representatives)
    matched = match_timeline(result)
    monthly = match_timeline_months(raw or {}, month_inputs, preserve_uncertainty=has_query,
                                    aspect=aspect, aspect_subject=aspect_subject, price=price)
    summary = (match_timeline_summary(raw, inputs, params['keywords'], summary_events,
                                      verified_months=monthly['months']) if raw is not None else
               {'lead': None, 'summary_status': '생략', 'summary_attempts': 0, 'summary_checks': []})
    timeline = {**matched, **monthly, **summary, 'summary_input': inputs, 'month_input': month_inputs,
                'llm_ms': llm_ms, 'llm_calls': llm_calls, "cached": False,
                'candidate_basis': ('기간 내 주요 사건' if digest else
                                    '핵심 결과 사건' if tiered and any(e.get('tier') == 'core' for e in retrieved) else
                                    '연관 결과 사건' if tiered else
                                    '필수 키워드 일치 사건의 주제 유사도와 보도량' if params['keywords'] and has_query else
                                    '필수 키워드 일치 사건의 묶음 전체 보도량' if params['keywords'] else
                                    'HNSW 검색 결과'),
                'candidate_ms': candidate_ms,
                'candidate_count': retrieved_count, 'filtered_candidate_count': filtered_count,
                'filter_relaxed': filter_relaxed,
                'query_min_cos': query_min_cos,
                'merged_count': len(merged), 'merge_ms': merge_ms,
                'relevance_status': relevance_status, 'relevance_ms': relevance_ms, 'relevance_excluded_count': excluded,
                'no_relevant_events': has_query and not matched['items'],
                'generation_ms': (perf_counter() - started) * 1000}
    if raw is not None or (cover_months and not candidates) or (
            relevance_status == '판정 완료' and (not eligible or relevance_calls == 2)):
        _cache_put(_timeline_cache, key, timeline)
    return timeline


class SearchError(Exception):
    """접속값·원본 예외를 담지 않는 공개 오류."""


def normalized_article_title_sql(title: str) -> str:
    whitespace = r'\0009-\000D\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000\FEFF'
    spaces = rf"U&'[\0020{whitespace}]+'"
    changes = rf"U&'[{whitespace}]|^ | $|  '"
    # 정규 제목은 그대로 써 전체 기사에 대한 정규식 치환을 줄인다.
    return (f'''(CASE WHEN {title} IS NFC NORMALIZED AND {title} COLLATE "C" !~ {changes}
                     THEN {title} ELSE btrim(regexp_replace(normalize({title}), {spaces}, ' ', 'g')) END)''')


def article_duplicate_ids(conn, date_from=None, date_to=None) -> list[int]:
    """발행일·정규화 제목·언론사별 최저 ID를 제외한 기사 ID를 읽는다."""
    calendar, values = article_filter({'date_from': date_from, 'date_to': date_to}, dedupe=False)
    # 같은 날짜에서 정규화 제목이 같은 행은 정렬 후 서로 인접한다. 이 행만 언론사를 조회한다.
    rows = conn.execute(f'''
        WITH scoped AS (
            SELECT a.id, a.published_date, {normalized_article_title_sql('a.title')} COLLATE "C" AS title_key
            FROM articles a WHERE TRUE {calendar}
        ), neighbors AS (
            SELECT *, lag(title_key) OVER title_order AS previous_title,
                      lead(title_key) OVER title_order AS next_title
            FROM scoped
            WINDOW title_order AS (PARTITION BY published_date ORDER BY title_key)
        ), ranked AS (
            SELECT r.id, row_number() OVER (
                PARTITION BY r.published_date, r.title_key, s.publisher ORDER BY r.id
            ) AS position
            FROM neighbors r
            LEFT JOIN LATERAL (SELECT publisher FROM article_sources WHERE article_id=r.id OFFSET 0) s ON TRUE
            WHERE r.title_key IN (r.previous_title, r.next_title)
        )
        SELECT id FROM ranked WHERE position > 1
    ''', values).fetchall()
    return [row['id'] for row in rows]


def duplicate_filter(params: dict, id_column: str) -> tuple[str, tuple]:
    duplicates = params.get('_duplicate_article_ids')
    # ID는 모두 NOT NULL이다. 해시 서브플랜으로 기사마다 전체 배열을 순회하는 비용을 피한다.
    return (f' AND {id_column} NOT IN (SELECT unnest(%s::bigint[]))', (duplicates,)) if duplicates else ('', ())


_duplicate_snapshot_lock = Lock()
_duplicate_snapshot: tuple[tuple, tuple[int, ...]] | None = None
_search_result_cache = OrderedDict()
SEARCH_RESULT_CACHE_SECONDS = 300
SEARCH_RESULT_CACHE_LIMIT = 64
ARTICLE_SNAPSHOT_TABLES = ('articles', 'article_sources', 'article_keywords', 'article_groups',
                           'article_categories', 'article_embeddings')
_UNREAD_SNAPSHOT = object()
_article_versions_lock = Lock()
_article_version_server = None
_article_pending_writers: dict[int, set[int]] = {}
_article_commit_versions = [0] * len(ARTICLE_SNAPSHOT_TABLES)


def article_data_snapshot(conn) -> tuple[tuple | None, tuple | None]:
    """중복 판정과 검색에 필요한 테이블의 변경 키를 각각 읽는다."""
    with _article_versions_lock:
        return _read_article_versions(conn)


def _read_article_versions(conn) -> tuple[tuple | None, tuple | None]:
    global _article_version_server
    row = conn.execute('''
        WITH tracked AS (
            SELECT rel, position FROM unnest(%s::regclass[]) WITH ORDINALITY t(rel, position)
        ), locks AS MATERIALIZED (
            SELECT locktype, relation, mode, virtualtransaction, transactionid FROM pg_locks
            WHERE granted AND (
                (locktype = 'transactionid' AND mode = 'ExclusiveLock') OR
                (locktype = 'relation' AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                 AND relation IN (SELECT rel FROM tracked)
                 AND mode IN ('RowExclusiveLock', 'ShareRowExclusiveLock', 'ExclusiveLock', 'AccessExclusiveLock'))
            )
        ), writer_ids AS (
            SELECT DISTINCT ON (virtualtransaction) virtualtransaction, transactionid::text::bigint AS xid
            FROM locks WHERE locktype = 'transactionid'
            ORDER BY virtualtransaction, age(transactionid) DESC
        ), snapshot AS (
            SELECT pg_current_snapshot() AS mvcc
        ), writers AS (
            -- 잠금의 32비트 XID를 현재 스냅샷의 epoch에 맞춰 복원한다.
            SELECT t.position, s.mvcc,
                   x.xmax - ((x.xmax - w.xid + 2147483648) & 4294967295) + 2147483648 AS xid
            FROM tracked t JOIN locks l ON l.relation = t.rel
            JOIN writer_ids w USING (virtualtransaction) CROSS JOIN snapshot s
            CROSS JOIN LATERAL (SELECT pg_snapshot_xmax(s.mvcc)::text::bigint AS xmax) x
            WHERE l.mode = 'RowExclusiveLock'
        )
        SELECT ARRAY[pg_postmaster_start_time()::text, inet_server_addr()::text, inet_server_port()::text,
                     current_database(), current_user::text] AS server,
               (SELECT max(id) FROM articles) AS last_article,
               (SELECT max(article_id) FROM article_embeddings) AS last_embedding,
               current_setting('track_counts') = 'on' AND NOT pg_is_in_recovery() AS enabled,
               ARRAY(SELECT ARRAY[t.rel::bigint, pg_relation_filenode(t.rel)::bigint,
                                  pg_stat_get_tuples_inserted(t.rel), pg_stat_get_tuples_updated(t.rel),
                                  pg_stat_get_tuples_deleted(t.rel)]
                     FROM tracked t ORDER BY t.position) AS versions,
               ARRAY(SELECT row_security_active(t.rel) OR EXISTS (
                         SELECT 1 FROM locks l WHERE l.relation = t.rel AND l.mode <> 'RowExclusiveLock'
                     ) OR EXISTS (
                         SELECT 1 FROM writers w WHERE w.position = t.position
                         AND pg_visible_in_snapshot(w.xid::text::xid8, w.mvcc)
                     )
                     FROM tracked t ORDER BY t.position) AS unsafe,
               ARRAY(SELECT DISTINCT ARRAY[w.xid, w.position - 1] FROM writers w
                     WHERE NOT pg_visible_in_snapshot(w.xid::text::xid8, w.mvcc)) AS pending_writers
    ''', (list(ARTICLE_SNAPSHOT_TABLES),)).fetchone()
    if not row or row.get('enabled') is not True:
        return None, None
    server = tuple(row['server'])
    if _article_version_server != server:
        _article_version_server = server
        _article_pending_writers.clear()
        _article_commit_versions[:] = [0] * len(ARTICLE_SNAPSHOT_TABLES)
    active = set()
    # 스냅샷에 안 보이는 쓰기는 기존 결과에 영향을 주지 않는다.
    for xid, table in row['pending_writers']:
        _article_pending_writers.setdefault(xid, set()).add(table)
        active.add(xid)
    finished = _article_pending_writers.keys() - active
    if finished:
        # 관측한 쓰기의 커밋은 비동기 테이블 통계보다 먼저 반영한다.
        states = conn.execute('''
            SELECT xid, pg_xact_status(xid::text::xid8) AS status FROM unnest(%s::bigint[]) AS t(xid)
        ''', (list(finished),)).fetchall()
        for writer in states:
            if writer['status'] == 'in progress':
                continue
            tables = _article_pending_writers.pop(writer['xid'])
            if writer['status'] != 'aborted':
                for table in tables:
                    _article_commit_versions[table] += 1
    versions = tuple((*values, version) for values, version in zip(row['versions'], _article_commit_versions))
    base = (server, row['last_article'])
    duplicate_key = (*base, *versions[:2]) if not any(row['unsafe'][:2]) else None
    result_key = (*base, row['last_embedding'], *versions) if duplicate_key is not None and not any(row['unsafe']) else None
    if os.environ.get('SEARCH_SNAPSHOT_DIAGNOSTICS') == '1':
        logger.warning('article snapshot pid=%s article=%s embedding=%s pending=%s unsafe=%s commits=%s',
                       os.getpid(), row['last_article'], row['last_embedding'], row['pending_writers'],
                       row['unsafe'], _article_commit_versions)
    return duplicate_key, result_key


def stable_article_data_snapshot(conn, prior) -> tuple[tuple | None, tuple | None]:
    # 통계가 MVCC 스냅샷보다 새로워지는 순간에는 이전 행을 새 키로 저장하지 않는다.
    duplicate_key, result_key = article_data_snapshot(conn)
    return (duplicate_key if duplicate_key == prior[0] else None,
            result_key if result_key == prior[1] else None)


def snapshot_duplicate_ids(conn, key=_UNREAD_SNAPSHOT) -> list[int]:
    """발행일·제목·언론사 판정에 영향을 주는 테이블만 중복 캐시 키에 넣는다."""
    global _duplicate_snapshot
    if key is _UNREAD_SNAPSHOT:
        key, _ = article_data_snapshot(conn)
    if key is None:
        return article_duplicate_ids(conn)
    with _duplicate_snapshot_lock:
        cached = _duplicate_snapshot
        if cached is not None and cached[0] == key:
            return list(cached[1])
        ids = article_duplicate_ids(conn)
        _duplicate_snapshot = (key, tuple(ids))
        if os.environ.get('SEARCH_SNAPSHOT_DIAGNOSTICS') == '1':
            logger.warning('duplicate snapshot ready pid=%s ids=%s', os.getpid(), len(ids))
    return ids


def cached_search_result(key):
    with _cache_lock:
        entry = _search_result_cache.get(key)
        if entry is None:
            return None
        expires, result, stats_plan = entry
        if monotonic() >= expires:
            del _search_result_cache[key]
            return None
        _search_result_cache.move_to_end(key)
    return deepcopy(result), stats_plan


def cache_search_result(key, result, stats_plan):
    entry = (monotonic() + SEARCH_RESULT_CACHE_SECONDS, deepcopy(result), stats_plan)
    with _cache_lock:
        _search_result_cache[key] = entry
        _search_result_cache.move_to_end(key)
        while len(_search_result_cache) > SEARCH_RESULT_CACHE_LIMIT:
            _search_result_cache.popitem(last=False)


def warm_duplicate_snapshot() -> None:
    """Warm the shared duplicate cache using a read-only snapshot."""
    env_file = Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV)))
    with psycopg.connect(
        **connection_options(env_file), autocommit=True, row_factory=dict_row,
        connect_timeout=10, application_name="dice_duplicate_warmup",
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
    ) as conn:
        conn.read_only = True
        prior = article_data_snapshot(conn)
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL jit = off; SET LOCAL work_mem = '32MB'; SET LOCAL enable_parallel_hash = off")
            if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
                raise SearchError("Read-only transaction could not be verified.")
            key, _ = stable_article_data_snapshot(conn, prior)
            if key is not None:
                snapshot_duplicate_ids(conn, key)


def with_article_duplicates(conn, params: dict, *, unbounded: bool = False, reuse_snapshot: bool = False,
                            snapshot_key=_UNREAD_SNAPSHOT) -> dict:
    bounds = () if unbounded else (params.get('date_from'), params.get('date_to'))
    ids = (snapshot_duplicate_ids(conn, snapshot_key) if reuse_snapshot and not any(bounds)
           else article_duplicate_ids(conn, *bounds))
    return {**params, '_duplicate_article_ids': ids}


def article_filter(params: dict, alias: str = 'a', *, correlated_publisher: bool = False,
                   dedupe: bool = True) -> tuple[str, tuple]:
    """검색/통계/타임라인에서 공유하는 발행일·언론사 SQL. 값은 모두 바인딩한다."""
    clauses, values = [], []
    for key, op in (('date_from', '>='), ('date_to', '<=')):
        if params.get(key):
            clauses.append(f'{alias}.published_date {op} %s::date')
            values.append(params[key])
    if params.get('publisher'):
        # 필수 키워드 통계는 선별된 기사 ID마다 언론사를 조회해 세미 조인으로 풀리는 것을 막는다.
        offset = ' OFFSET 0' if correlated_publisher else ''
        if params['publisher'] == OTHER_PUBLISHERS:
            major = major_publishers(list_publishers())
            if not major:
                raise SearchError('주요 언론사 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.')
            condition, publisher_value = 'NOT (fs.publisher = ANY(%s::text[]))', major
        else:
            condition, publisher_value = 'fs.publisher=%s', params['publisher']
        clauses.append(f'EXISTS (SELECT 1 FROM article_sources fs WHERE fs.article_id={alias}.id AND {condition}{offset})')
        values.append(publisher_value)
    if params.get('mode') == 'digest' and (category := news_digest_category(params['q'])):
        clauses.append(f'''EXISTS (SELECT 1 FROM article_categories c WHERE c.article_id={alias}.id
                                   AND ({NORMALIZED_MAJOR_SQL})=%s)''')
        values.append(category)
    unique, unique_values = duplicate_filter(params, f'{alias}.id') if dedupe else ('', ())
    scope = ' AND ' + ' AND '.join(clauses) if clauses else ''
    return scope + unique, (*values, *unique_values)


def missing_article_coverage(conn, params: dict) -> str | None:
    scope, values = article_filter(params, dedupe=False)
    if not scope:
        return None
    if conn.execute(f'SELECT EXISTS (SELECT 1 FROM articles a WHERE TRUE {scope}) AS present',
                    values).fetchone()['present'] is False:
        return 'scope_empty'
    return None


def article_coverage_notice(conn, params: dict) -> dict:
    row = conn.execute('SELECT min(published_date) AS date_from, max(published_date) AS date_to FROM articles').fetchone()
    start, end = row['date_from'], row['date_to']
    message = '해당 언론사·기간 기사가 DB에 없습니다.' if params.get('publisher') else '해당 기간 기사가 DB에 없습니다.'
    category = params.get('mode') == 'digest' and news_digest_category(params.get('q', ''))
    if category:
        # 분류로 거른 결과가 비면 '기사 없음'이 아니라 그 분류의 보유 기간을 안내한다(예: 네이버 수집분엔 '지역' 분류 없음).
        cat = conn.execute(f'''SELECT min(a.published_date) AS date_from, max(a.published_date) AS date_to
                               FROM articles a JOIN article_categories c ON c.article_id=a.id
                               WHERE ({NORMALIZED_MAJOR_SQL})=%s''', (category,)).fetchone()
        message = f"해당 기간에는 '{category}' 분류 기사가 없습니다."
        start, end = cat['date_from'], cat['date_to']
        if start and end:
            message += f" '{category}' 분류 데이터는 {start}~{end}만 있습니다."
        return {'data_coverage': row, 'data_absence_message': message}
    if start and end:
        message += f' 데이터 보유 기간은 {start}~{end}입니다.'
    return {'data_coverage': row, 'data_absence_message': message}


def validate_params(params: dict) -> dict:
    if not isinstance(params, dict):
        raise SearchError("검색 조건은 JSON 객체여야 합니다.")
    if set(params) - {"keywords", "mode", "q", "limit", "date_from", "date_to", "publisher"}:
        raise SearchError("지원하는 검색 조건은 keywords, q, limit, date_from, date_to, publisher와 이전 mode입니다.")
    mode = params.get("mode")
    if mode is not None and mode not in ("keyword", "topic"):
        raise SearchError("mode는 keyword 또는 topic이어야 합니다.")
    q = params.get("q", "")
    if not isinstance(q, str) or len(q) > 200:
        raise SearchError("q는 200자 이내의 주제 문장이어야 합니다.")
    raw = params.get("keywords", "")
    if mode == "keyword" and "keywords" not in params:
        raw, q = q, ""
    if isinstance(raw, list) and all(isinstance(k, str) for k in raw):
        raw = " ".join(raw)
    if not isinstance(raw, str) or len(raw) > 100:
        raise SearchError("keywords는 쉼표·공백 구분 문자열 또는 문자열 목록이어야 합니다(전체 100자 이내).")
    raw = FOUR_DAY_WORKWEEK.sub('주4일제', raw)
    for name, aliases in ENTITY_ALIASES.items():
        for alias in aliases:
            if ' ' in alias:
                raw = re.sub(r'\s+'.join(map(re.escape, alias.split())), name, raw)
    keywords = list(dict.fromkeys(canonical_keyword(k) for k in re.split(r"[,\s]+", raw.strip()) if k))
    if len(keywords) > 30 or any(len(k) > 100 for k in keywords):
        raise SearchError("필수 키워드는 최대 30개, 각 100자 이내로 입력하세요.")
    limit = params.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise SearchError(f"limit은 1~{MAX_LIMIT} 사이 정수여야 합니다.")
    q = q.strip()
    if keywords in ([], ['삼성'], ['삼성전자']):
        stock_topic = SAMSUNG_STOCK_ALIAS.sub('삼성전자', q)
        if stock_topic != q:
            q, keywords = stock_topic, ['삼성전자']
    if q in REGISTERED_PERSON_NAMES and (not keywords or keywords == [q]):
        keywords, q = [q], ''
    names = query_entities(q)
    if national_team_sports(q, keywords) and '대표팀' in keywords:
        keywords = list(dict.fromkeys('국가대표' if k == '대표팀' else k for k in keywords))
    if names and (not keywords or any(name in TEAM_NAMES for name in names) or context_words(q) & PERSON_CONTEXT_WORDS):
        keywords = list(dict.fromkeys([*names, *(k for k in keywords if k.casefold() not in CONTEXT_EXCLUSIONS)]))
    elif TEAM_NAMES.intersection(keywords):
        keywords = [k for k in keywords if k.casefold() not in CONTEXT_EXCLUSIONS]
    filters = {}
    for key in ('date_from', 'date_to'):
        value = params.get(key) or None
        if value is not None:
            try:
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError()
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                raise SearchError('기간은 YYYY-MM-DD 형식의 실제 날짜여야 합니다.') from None
        filters[key] = value
    if filters['date_from'] and filters['date_to'] and filters['date_from'] > filters['date_to']:
        raise SearchError('시작일은 종료일보다 늦을 수 없습니다.')
    publisher = params.get('publisher') or None
    if publisher is not None and (not isinstance(publisher, str) or len(publisher) > 50):
        raise SearchError('언론사는 50자 이내 이름이어야 합니다.')
    filters['publisher'] = publisher.strip() or None if publisher else None
    mode = "combined" if keywords and q else "topic" if q else "keyword" if keywords else "empty"
    if mode == 'topic' and (filters['date_from'] or filters['date_to']) and news_digest_category(q) is not None:
        mode = 'digest'
    return {"mode": mode, "q": q, "keywords": keywords, "limit": limit, **filters}


def _encode_query(q: str) -> str:
    global _model
    with _model_lock:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        if _model is None:
            _model = load_model(DEFAULT_CACHE)
        vector = encode_titles(_model, [q], 1)[0]
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


def start_model_warmup() -> Thread:
    """모델과 첫 CUDA 추론을 백그라운드에서 준비한다. 중복 시작은 하지 않는다."""
    global _warmup_thread
    with _warmup_lock:
        if _warmup_thread is None:
            def warmup():
                global _warmup_failed
                try:
                    _encode_query("검색 모델 준비")
                except Exception:
                    _warmup_failed = True
                finally:
                    _warmup_done.set()
            _warmup_thread = Thread(target=warmup, name="search-model-warmup", daemon=True)
            _warmup_thread.start()
        return _warmup_thread


def query_embedding(q: str) -> str:
    if _warmup_thread is not None:
        _warmup_done.wait()
        if _warmup_failed:
            raise SearchError("주제 모델 사전 로드 실패: 로컬 모델 캐시·CUDA를 확인하고 서버를 재시작하세요.")
    return _encode_query(q)


def keyword_source(keywords: list[str], topic: str = '', *, fast_titles: bool = False) -> tuple[str, tuple]:
    # 별칭·등록 인물·직함으로 식별한 인물에 제목 조회를 추가한다.
    names = set(query_entities(topic))
    sources, values = [], []
    for keyword in keywords:
        aliases = ENTITY_ALIASES.get(keyword)
        if keyword in COMPANY_ALIASES:
            aliases = core_keyword_forms(keyword)
        if keyword == '국가대표' and national_team_sports(topic, keywords):
            aliases = NATIONAL_TEAM_ALIASES
        latin = SHORT_LATIN_KEYWORD.fullmatch(keyword)
        if aliases or keyword in names or keyword in REGISTERED_PERSON_NAMES or latin:
            aliases = aliases or (keyword,)
            variants = {keyword.upper()} if latin else set()
            pattern = term_title_pattern(keyword) if keyword in COMPANY_ALIASES else title_entity_pattern(aliases)
            if fast_titles:
                title_scan, title_values = title_scan_check(keyword, pattern, aliases=aliases)
                sources.append(f'''(SELECT article_id FROM article_keywords WHERE keyword=ANY(%s::text[])
                                     UNION SELECT a.id FROM articles a WHERE {title_scan})''')
            else:
                sources.append('''(SELECT article_id FROM article_keywords WHERE keyword=ANY(%s::text[])
                                    UNION SELECT id FROM articles WHERE title ~* %s)''')
                title_values = (pattern,)
            values.extend((sorted({*variants, *aliases, *(a.lower() for a in aliases)}), *title_values))
        else:
            spaced = [form for form in CORE_KEYWORD_ALIASES.get(keyword, ()) if ''.join(form.split()) == keyword]
            if spaced:
                sources.append('SELECT article_id FROM article_keywords WHERE keyword=ANY(%s::text[])')
                values.append([keyword, *spaced])
            else:
                sources.append('SELECT article_id FROM article_keywords WHERE keyword=%s')
                values.append(keyword)
    return ' INTERSECT '.join(sources), tuple(values)


def business_wrong_title(name_pattern: str) -> str:
    return rf'{BASEBALL_CONTEXT}|(?:{name_pattern})\s+(?:영화\s*)?감독|예술인상'


def focused_title_signals(topic: str) -> tuple[str, str, str, str] | None:
    if name := re.search(r'(?<![가-힣])([가-힣]{2,4})\s+(?:재판|공판)\s*(?:의\s*)?(?:결과|선고|판결)', topic):
        subject = re.escape(name[1])
        trial = rf'{subject}(?:\s*(?:대통령|전 대표|지사))?(?:의)?\s*(?:재판|공판|항소심|상고심)'
        return (subject, trial, rf'{subject}\s*(?:캠프|측근)|촉구|목소리|해야|하라|여론조사',
                rf'(?:{trial}).{{0,12}}(?:중단|연기|정지|선고|판결|형량|징역|무죄|유죄)')
    if all(word in topic for word in ('월드컵', '한국', '일정')):
        return (r'한국|대표팀|홍명보|대한민국', r'(?=.*월드컵)(?=.*(?:일정|첫\s*(?:경기|상대)|\d+\s*시|킥오프))',
                r'농구|배구|탁구|U[- ]?20|여자|여성|예측|탈락|첫\s*경기(?:에)?서',
                r'일정|\d+\s*시|킥오프|생중계|치른다')
    return None


def title_signals(params: dict) -> tuple[str, str, str, str] | None:
    topic = params.get('q', '')
    aspect = topic_aspect_pattern(topic)
    if not aspect and (focused := focused_title_signals(topic)):
        return focused
    entities = query_entities(topic)
    names = [k for k in params['keywords'] if k in ENTITY_ALIASES or k in entities]
    if aspect and not names:
        names = params['keywords']
    if not names or not topic:
        return None
    sports = national_team_sports(topic, params['keywords'])
    aliases = [alias for k in names
               for alias in (NATIONAL_TEAM_ALIASES if k == '국가대표' and sports else ENTITY_ALIASES.get(k, (k,)))]
    words = context_words(topic)
    if words & BUSINESS_CONTEXT_WORDS:
        words.add('기업인')
    positive = '|'.join(pattern for context, pattern in CONTEXT_PATTERNS.items() if context in words)
    focus = '|'.join(pattern for context, pattern in CONTEXT_FOCUS.items() if context in words)
    if aspect:
        positive = aspect
        if '하락' in topic or '급락' in topic:
            focus = rf'(?=.*(?:{aspect})).*(?:하락|급락|약세|하향|내려|매도)'
        elif '상승' in topic or '급등' in topic:
            focus = rf'(?=.*(?:{aspect})).*(?:상승|급등|강세|상향|올라|매수)'
    elif sports:
        positive = '|'.join(SPORT_TITLE_PATTERNS[sport] for sport in sports)
        if '불화설' in topic:
            focus = r'불화|갈등|충돌|하극상'
    name_pattern = title_entity_pattern(aliases)
    negative = (AUTO_CONTEXT if TEAM_NAMES.intersection(names) else
                business_wrong_title(name_pattern) if '기업인' in words else '')
    return name_pattern, positive or r'(?!)', negative or r'(?!)', focus or r'(?!)'


def topic_title_focus(topic: str) -> tuple[str, str, str, str] | None:
    """제목에서 핵심 대상과 쟁점을 함께 확인하는 짧은 복합 주제."""
    words = set(QUERY_WORD.findall(topic))
    if '관세' in words and words & {'트럼프', '한미'}:
        subject = r'트럼프|한미|미국|美|백악관'
        focus = r'협상|협의|합의|타결|담판|논의' if '협상' in words else r'트럼프\s*(?:의\s*)?관세'
        return subject, r'관세|통상', r'(?!)', focus
    if '전당대회' in words and words & {'국민의힘', '국힘'}:
        return r'국민의힘|국힘', r'전당\s*대회|당대표|당권\s*경쟁|대표\s*선출', r'(?!)', r'전당\s*대회'
    if '규제' in words and ({'인공지능', 'ai'} & {word.casefold() for word in words}):
        subject = title_entity_pattern(('인공지능', '인공 지능', 'AI'))
        return subject, r'규제|감독\s*기관', r'(?!)', r'규제'
    if re.search(rf'(?<!\w)실적(?:{ALIAS_PARTICLE})?(?!\w)', topic):
        subject = title_entity_pattern(ENTITY_ALIASES['SK하이닉스'])
        if re.search(subject, topic, re.IGNORECASE):
            earnings = r'실적|영업\s*(?:익|이익)|매출'
            other_company = r'\s*(?:\[[^\]]+\]\s*)?SK\s*스퀘어'
            context = rf'^(?!{other_company})(?=.*(?:{subject})).*(?:{earnings}|[1-4]\s*분기)'
            return subject, context, rf'^{other_company}', earnings
    return None


def timeline_title_signals(params: dict) -> tuple[tuple[str, str, str, str] | None, str]:
    """등록 단서와 주제어를 후보 순위 및 대표 제목 선택에 사용한다."""
    signals = title_signals(params)
    if aspect := topic_aspect_pattern(params['q']):
        return signals, aspect
    if focus := topic_title_focus(params['q']):
        return focus, focus[1]
    terms = timeline_aspect_terms(params)
    if not terms or not params['keywords']:
        return signals, ''
    aspect = ''.join(rf'(?=.*{re.escape(term)})' for term in terms)
    name = title_entity_pattern(alias for keyword in params['keywords']
                                for alias in ENTITY_ALIASES.get(keyword, (keyword,)))
    return (name, aspect, r'(?!)', r'(?!)'), aspect


def literal_title_focus(params: dict) -> tuple[str, str, str, str] | None:
    topic = params['q']
    keyword = canonical_keyword(topic)
    if params['keywords'] == [keyword] and (SHORT_LATIN_KEYWORD.fullmatch(topic) or keyword == '오징어게임'):
        pattern = title_entity_pattern(ENTITY_ALIASES.get(keyword, (keyword,)))
        return pattern, pattern, r'(?!)', r'(?!)'
    return None


def additional_title_pattern(params: dict) -> str:
    """필수어 밖의 핵심 표현이 제목에 있으면 순위에서 가산한다."""
    aliases = {alias for keyword in params['keywords'] for alias in ENTITY_ALIASES.get(keyword, (keyword,))}
    if not aliases:
        return ''
    patterns, required_parts = [], set()
    for alias in sorted(aliases, key=lambda word: (-len(word), word)):
        parts = QUERY_WORD.findall(alias)
        pattern = r'\s*'.join(map(re.escape, parts)) if ''.join(parts) == ''.join(alias.split()) else re.escape(alias)
        patterns.append(pattern)
        required_parts.update(part.casefold() for part in parts)
    names = '|'.join(patterns)
    remainder, removed = re.subn(rf'(?<!\w)(?:{names})(?:{ALIAS_PARTICLE})?(?!\w)', ' ', params['q'],
                                 flags=re.IGNORECASE)
    if not removed:
        return ''
    terms = {word for word in QUERY_WORD.findall(remainder)
             if len(word) >= 2 and not word.isdecimal() and word not in WEB_FILLER
             and word.casefold() not in required_parts}
    return title_entity_pattern(sorted(terms)) if 1 <= len(terms) <= 3 else ''


def category_flag(categories: list[str], *, mismatch: bool = False) -> tuple[str, tuple]:
    """기사의 모든 분류가 조건을 충족할 때만 표시한다. 빈 분류는 보호한다."""
    comparison = '<> ALL' if mismatch else '= ANY'
    values = [MAJOR_CATEGORY_ALIASES.get(category, category) for category in categories]
    if mismatch:
        values.append('미분류')
    return (f"""COALESCE((SELECT bool_and(COALESCE(({NORMALIZED_MAJOR_SQL}) {comparison} (%s::text[]), FALSE))
                FROM article_categories c WHERE c.article_id=a.id), FALSE)""", (values,))


def category_mismatch_flag(params: dict) -> tuple[str, tuple]:
    target = params.get('target_category')
    return category_flag([target], mismatch=True) if target and not params.get('excluded_categories') else ('', ())


def category_rank_sql(score: str, mismatch: str = 'category_mismatch') -> str:
    return f'{score} - {CATEGORY_RANK_PENALTY} * {mismatch}::int'


def category_filter(params: dict) -> tuple[str, tuple]:
    excluded = params.get('excluded_categories')
    if not excluded or national_team_sports(params.get('q', ''), params.get('keywords', ())):
        return '', ()
    flag, values = category_flag(excluded)
    return f' AND NOT {flag}', values


def person_title_flag(params: dict) -> tuple[str, tuple]:
    topic = params.get('q', '')
    name = fixed_person_name(topic)
    if not name or not params.get('excluded_categories'):
        return '', ()
    words = context_words(topic)
    name_pattern = title_entity_pattern((name,))
    if words & POLITICAL_CONTEXT_WORDS and not words & PERSON_TITLE_WORDS:
        pattern = PERSON_TITLE_PATTERNS['정치인'].replace('{name}', name_pattern)
        return 'a.title ~* %s', (pattern,)
    if words & BUSINESS_CONTEXT_WORDS:
        return '(a.title ~* %s AND a.title ~* %s AND a.title !~* %s)', (
            name_pattern, PERSON_TITLE_PATTERNS['기업인'], business_wrong_title(name_pattern),
        )
    if '야구선수' in words:
        return (f'(a.title ~* %s AND a.title !~* %s AND (a.title ~* %s OR {BASEBALL_CATEGORY_SQL}))',
                (name_pattern, AUTO_CONTEXT, SPORT_TITLE_PATTERNS['야구']))
    return '', ()


def required_title_flag(params: dict) -> tuple[str, tuple]:
    sports = national_team_sports(params.get('q', ''), params.get('keywords', ()))
    if not sports:
        return person_title_flag(params)
    selected = '|'.join(SPORT_TITLE_PATTERNS[sport] for sport in sports)
    other_sports = '|'.join(pattern for sport, pattern in SPORT_TITLE_PATTERNS.items() if sport not in sports)
    return (f"""(a.title !~* %s AND ((a.title ~* %s AND a.title !~* %s) OR EXISTS (
        SELECT 1 FROM article_categories c WHERE c.article_id=a.id AND ({NORMALIZED_MAJOR_SQL})='스포츠'
    )))""", (other_sports or r'(?!)', selected, NONSPORT_TITLE))


def context_filter(params: dict) -> tuple[str, tuple]:
    conditions, values = category_filter(params)
    title_flag, title_values = required_title_flag(params)
    if title_flag:
        conditions += f' AND {title_flag}'
    return conditions, (*values, *title_values)


def timeline_context_filter(params: dict) -> tuple[str, tuple]:
    conditions, values = context_filter(params)
    teams = TEAM_NAMES.intersection(params.get('keywords', ()))
    if not teams:
        return conditions, values
    aliases = [alias for team in sorted(teams) for alias in ENTITY_ALIASES[team]]
    conditions += f''' AND a.title !~* %s AND a.title !~* %s
        AND (a.title ~* %s OR a.title ~* %s OR {BASEBALL_CATEGORY_SQL})'''
    return conditions, (*values, WEATHER_DIGEST, AUTO_CONTEXT, title_entity_pattern(aliases), SPORT_TITLE_PATTERNS['야구'])


def timeline_article_filter(params: dict, *, dedupe: bool = True) -> tuple[str, tuple]:
    conditions, values = article_filter(params, dedupe=dedupe)
    context, context_values = timeline_context_filter(params)
    return conditions + context, (*values, *context_values)


def timeline_params(result: dict) -> dict:
    return {**result['params'], **result.get('term_partition', {}),
            **{k: result[k] for k in ('excluded_categories', 'target_category') if result.get(k)}}


def timeline_context_label(params: dict) -> str:
    return params['q'] if (params.get('excluded_categories') or params.get('target_category')
                           or topic_aspect_pattern(params.get('q', ''))
                           or national_team_sports(params.get('q', ''), params.get('keywords', ()))) else ''


CORE_FILLER = TIMELINE_ASPECT_FILLER | WEB_FILLER | {
    '위한', '위해', '열리는', '최적', '최적의', '추천', '사유', '분석', '원인', '이슈',
    '내린다던데', '받았어', '근황',
}
CORE_CUES = {
    '미국': r'미국|美|백악관',
    '이적': r'이적|영입|새\s*팀',
    '경질': title_entity_pattern(('경질', '경질된', '경질되는', '경질됐다', '경질한', '경질했다'), ignore_spacing=True),
    '지지율': r'지지율|국정\s*(?:지지|평가)|직무\s*수행\s*평가',
    '하락': r'하락|급락|약세|하향|내려|내렸|매도|사이드카|떨어',
    '급락': r'급락|하락|약세|사이드카',
    '순위': CONTEXT_PATTERNS['순위'] + r'|포스트\s*시즌',
    '불화설': r'불화|갈등|충돌|하극상',
    '국가대표': title_entity_pattern(NATIONAL_TEAM_ALIASES) + r'|손흥민|이강인',
    '노벨문학상': r'노벨\s*(?:문학상|상)',
    '노벨상': r'노벨\s*(?:문학상|상)',
    '신제품': r'신제품|공개|출시|언팩',
    '금리': r'금리|금통위',
    '인공지능': r'인공\s*지능|(?<![A-Za-z0-9])AI(?![A-Za-z0-9])',
    '국민의힘': r'국민의힘|국힘',
    '국힘': r'국민의힘|국힘',
    '전당대회': r'전당\s*대회|당대표|당권\s*경쟁|대표\s*선출',
    '일정': r'일정|조별\s*리그|킥오프|생중계|\d+\s*시|치른다',
    '한국': r'한국|대한민국|대표팀|홍명보',
    '대회': r'대회|마라톤|레이스',
    '코스': r'코스|여행지|명소|추천|휴양지|관광지|가볼\s*만한',
    '솔로': r'솔로|싱글|혼자|1인',
    '크리스마스': r'크리스마스|성탄(?:절)?',
    '전세사기': r'전세\s*사기',
    '개인정보': r'개인\s*정보',
    '배드민턴': r'배드민턴|셔틀콕',
    '유가': r'유\s*가(?!\s*사\s*지\s*론)',
}
CORE_KEYWORD_ALIASES = {
    '사이버렉카': ('사이버 렉카',),
    '주4일제': ('주 4일제',),
    '전세사기': ('전세 사기',), '개인정보': ('개인 정보',), '디어워즈': ('디 어워즈',),
    '크리스마스': ('성탄절', '성탄'), '노벨상': ('노벨문학상', '노벨 문학상'),
    '실적': ('매출', '영업이익', '영업익', '순이익'), '지지율': ('국정지지율', '국정평가'),
    '국민의힘': ('국힘',), '국힘': ('국민의힘',), '전당대회': ('전당 대회', '당권 경쟁'),
    '꽃값': ('꽃 값', '꽃가격', '꽃 가격'),
}
COMPANY_ALIASES = {
    '한국전력': '한전', '현대자동차': '현대차', '삼성전자': '삼전', 'SK텔레콤': 'SKT',
    '한국토지주택공사': 'LH', '국민건강보험공단': '건보공단', '더불어민주당': '민주당',
    '국민의힘': '국힘', '배달의민족': '배민',
}
COMPANY_ALIASES.update({short: name for name, short in COMPANY_ALIASES.items()})
COMPANY_ALIAS_KEYS = {word.casefold(): word for word in COMPANY_ALIASES}
QUERY_COMPOUND_ALIASES = {
    word: tuple(alias for alias in aliases if alias != word and ''.join(alias.split()) == word)
    for word, aliases in (ENTITY_ALIASES | CORE_KEYWORD_ALIASES).items()
}
QUERY_COMPOUND_ALIASES = {word: aliases for word, aliases in QUERY_COMPOUND_ALIASES.items() if aliases}


def core_keyword_forms(word: str) -> list[str]:
    aliases = (*ENTITY_ALIASES.get(word, (word,)), *CORE_KEYWORD_ALIASES.get(word, ()),
               *([COMPANY_ALIASES[word]] if word in COMPANY_ALIASES else []))
    return sorted({variant for alias in aliases for variant in (alias, alias.lower(), alias.upper())})


def core_person_names(params: dict, role: str | None) -> set[str]:
    return {name for name in (*params.get('person_names', ()), *TITLE_REQUIRED_PEOPLE, role)
            if name and web_word_forms(name).isdisjoint(NON_PERSON_ENTITIES)}


def core_terms(params: dict) -> dict[str, tuple[str, bool]]:
    """제목이나 기사 키워드에서 확인할 핵심어와 제목 필수 여부를 정한다."""
    topic = params['q']
    if role_terms := lexical_role_terms(topic):
        # 이전에 인물 질의로 해석되어 저장된 검색 조건도 같은 주제에서 다시 판정한다.
        words = dict.fromkeys(canonical_keyword(word) for word in (*params['keywords'], *role_terms))
        return {word: (term_title_pattern(word), False) for word in words}
    role = fixed_person_name(topic)
    people = core_person_names(params, role)
    terms = {}
    words = [*params['keywords']]
    remainder = normalize_query_term(without_query_context(topic), people)
    for keyword in words:
        aliases = sorted(ENTITY_ALIASES.get(keyword, (keyword,)), key=len, reverse=True)
        pattern = title_entity_pattern(aliases, ignore_spacing=True)
        remainder = re.sub(rf'(?<!\w)(?:{pattern})(?:{ALIAS_PARTICLE})?(?!\w)', ' ', remainder, flags=re.IGNORECASE)
    topic_words = TOPIC_WORD.findall(remainder)
    for word in topic_words:
        if word.isdecimal() or not web_word_forms(word).isdisjoint(CORE_FILLER):
            continue
        particle = QUERY_PARTICLE_SUFFIX.fullmatch(word)
        if (word not in people and particle and len(particle[1]) >= 2 and particle[2] not in ('이란', '란')
                and is_query_particle(*particle.groups())):
            word = particle[1]
        if role and word.casefold() in PERSON_CONTEXT_WORDS:
            continue
        if word in {'대통령', '작가'} and any(name and name in topic for name in people):
            continue
        words.append(word)
    # 질문이 '연구'·'시즌' 같은 일반어 한 단어뿐이면 그 단어를 핵심어로 쓴다(비우면 핵심 결과가 0건).
    if not words and len(single := TOPIC_WORD.findall(topic)) == 1 and not single[0].isdecimal():
        words.append(normalize_query_term(single[0], people))
    if holiday := HOLIDAY_CONTEXT.search(topic):
        subjects = [word for word in words if word not in (holiday[1], '연휴')]
        if subjects:
            words = [word for word in words if word in params['keywords'] or word in subjects]
    sports = national_team_sports(topic, params['keywords'])
    focus = topic_title_focus(topic)
    if focus and not params['keywords']:
        subject, context, _, detail = focus
        if '관세' in words:
            terms = {next(word for word in words if word in {'트럼프', '한미'}): (subject, False),
                     '관세': (context, False), **({'협상': (detail, False)} if '협상' in words else {})}
        elif '전당대회' in words:
            terms = {'국민의힘': (subject, False), '전당대회': (context, False)}
        elif '규제' in words:
            terms = {'인공지능': (subject, False), '규제': (context, False)}
        else:
            terms = {'SK하이닉스': (subject, False), '실적': (context, False)}
        return selected_core_terms(params, terms, people)
    for word in dict.fromkeys(canonical_keyword(normalize_query_term(word, people)) for word in words):
        pattern = term_title_pattern(word, person=word in people)
        if word in SPORT_TITLE_PATTERNS and sports:
            pattern = SPORT_TITLE_PATTERNS[word]
        terms[word] = (pattern, word in people)
    if '디어워즈' in terms:
        terms.pop('디어워', None)
        terms.pop('어워즈', None)
    return selected_core_terms(params, terms, people)


def selected_core_terms(params: dict, legacy: dict, people: set) -> dict[str, tuple[str, bool]]:
    terms = legacy.copy()
    if params.get('required_terms'):
        supporting = params.get('supporting_terms', [])
        required = [without_query_context(word) for word in params['required_terms']]
        required.extend(required_keyword_terms(params, supporting))
        source = params['q'] or ' '.join(params['keywords'])
        required = normalize_required_terms(list(filter(None, required)), supporting, source, people)
        if required:
            terms = {word: (legacy[word][0] if word in legacy else term_title_pattern(word, person=word in people),
                            word in people) for word in required}
    names = [word for word in terms if word in people]
    if not names:
        return terms
    for word in PERSON_DESCRIPTORS:
        terms.pop(word, None)
    for word in terms.keys() & {*TOPIC_ASPECTS, *QUERY_EVENT_ASPECTS, *PERSON_EVENT_ASPECTS}:
        terms[word] = (terms[word][0], True)
    if '경질' in terms:
        # ‘이승엽 절친 아베 감독 경질 위기’처럼 친분 소개에만 나온 이름을 제외한다.
        for name in names:
            terms[name] = (rf'(?:{terms[name][0]})(?!\s*(?:의\s*)?절친)', True)
    if '전역' in terms:
        # 지역 범위를 뜻하는 '서울 전역' 등의 단독 표기는 군 복무 종료의 근거가 아니다.
        name_pattern = '|'.join(terms[name][0] for name in names)
        terms['전역'] = (
            rf'(?:{name_pattern})(?:{SHORT_NAME_PARTICLE})?[”’"\',，\s]+전역|만기\s*전역|군\s*전역|'
            r'(?<![가-힣A-Za-z0-9])제대(?:[했한하]|(?![가-힣A-Za-z0-9]))|'
            r'전역(?:한|했다|한다|하는|하고|식|일|\s*(?:후|예정|신고|소감|현장|당일))', True,
        )
    # 성+씨 호칭은 제목에 생략돼도 저장된 키워드가 정확히 일치하면 근거로 인정한다.
    for name in surname_honorific_terms(params, terms):
        terms[name] = (terms[name][0], False)
    return terms


def short_person_names(params: dict) -> set[str]:
    return {name for name in params.get('person_names', ()) if re.fullmatch(r'[가-힣]', name)}


def surname_honorific_terms(params: dict, terms: dict) -> set[str]:
    return {name for name in params.get('person_names', ()) if name in terms and SURNAME_HONORIFIC.fullmatch(name)}


def term_title_pattern(word: str, *, person: bool = False) -> str:
    if person and re.fullmatch(r'[가-힣]', word):
        return (rf'(?<![가-힣A-Za-z0-9]){word}'
                rf'(?=(?:{SHORT_NAME_PARTICLE})?(?![가-힣A-Za-z0-9]))')
    if TOPIC_MONTH.fullmatch(word):
        return rf'(?<![0-9])0?{int(word[:-1])}\s*월'
    if word in COMPANY_ALIASES or word == '꽃값':
        return title_entity_pattern(core_keyword_forms(word), ignore_spacing=True)
    return TOPIC_ASPECTS.get(word) or CORE_CUES.get(word) or title_entity_pattern(
        ENTITY_ALIASES.get(word, (word,)), ignore_spacing=True)


def supporting_title_pattern(word: str) -> str:
    if TOPIC_MONTH.fullmatch(word):
        return term_title_pattern(word)
    return title_entity_pattern(core_keyword_forms(word), ignore_spacing=True)


def article_term_check(word: str, pattern: str, title_required: bool) -> tuple[str, tuple]:
    if title_required:
        return 'a.title ~* %s', (pattern,)
    return ('''(a.title ~* %s OR EXISTS (
        SELECT 1 FROM article_keywords k WHERE k.article_id=a.id AND k.keyword=ANY(%s::text[]) OFFSET 0
    ))''', (pattern, core_keyword_forms(word)))


def title_scan_check(word: str, pattern: str, *, aliases=None) -> tuple[str, tuple]:
    # 생성된 한글 패턴에만 필요한 음절을 먼저 검사한다. 공백·조사·경계는 기존 정규식으로 확인한다.
    forms = aliases if aliases is not None else ENTITY_ALIASES.get(word, (word,))
    if word in COMPANY_ALIASES or word == '꽃값':
        forms = core_keyword_forms(word)
    compact = [''.join(form.split()) for form in forms]
    if all(re.fullmatch(r'[가-힣]{2,30}', form) for form in compact) and pattern in (
            title_entity_pattern(forms, ignore_spacing=True), title_entity_pattern(forms)):
        likes = list(dict.fromkeys('%' + '%'.join(form) + '%' for form in compact))
        if len(likes) == 1:
            return 'CASE WHEN a.title NOT LIKE %s THEN FALSE ELSE a.title ~* %s END', (likes[0], pattern)
        return 'CASE WHEN a.title LIKE ANY(%s::text[]) THEN a.title ~* %s ELSE FALSE END', (likes, pattern)
    return 'a.title ~* %s', (pattern,)


def related_terms(params: dict, terms: dict) -> dict:
    short_names = short_person_names(params)
    anchored_names = short_names or surname_honorific_terms(params, terms)
    return {word: (pattern if word in short_names else title_entity_pattern(core_keyword_forms(word), ignore_spacing=True),
                   title_required)
            for word, (pattern, title_required) in terms.items() if not anchored_names or word in anchored_names}


def compound_related_terms(conn, params: dict, terms: dict) -> dict:
    """미등록 복합어를 기사 키워드로 분해해 가장 드문 구성어를 제목 단서로 쓴다."""
    if len(terms) != 1 or params.get('web_alias'):
        return {}
    word, = terms
    source = params['q'].strip(' ?？!.') or ' '.join(params['keywords'])
    if (source != word or not re.fullmatch(r'[가-힣]{5,30}', word) or terms[word][1]
            or word in ENTITY_ALIASES or word in CORE_KEYWORD_ALIASES or word in COMPANY_ALIASES
            or word in CORE_CUES or word in TOPIC_ASPECTS):
        return {}
    size = len(word)
    pieces = {word[start:end] for start in range(size - 1) for end in range(start + 2, size + 1)}
    rows = conn.execute('''
        SELECT word, n FROM unnest(%s::text[]) AS words(word)
        CROSS JOIN LATERAL (SELECT count(*) AS n FROM (
            SELECT 1 FROM article_keywords WHERE keyword=word LIMIT 50001
        ) AS bounded) AS counts WHERE n > 0
    ''', (sorted(pieces),)).fetchall()
    counts = {row['word']: row['n'] for row in rows}
    if word in counts:
        return {}
    splits = {size: ()}
    for start in range(size - 2, -1, -1):
        options = [(part, *suffix) for end, suffix in splits.items() if end >= start + 2
                   and (part := word[start:end]) in counts]
        if options:
            splits[start] = min(options, key=lambda parts: (len(parts), -sum(counts[p] for p in parts)))
    if parts := splits.get(0):
        # '시험'처럼 흔한 조각 하나만 맞는 기사가 들어오지 않도록 한다.
        anchor = min(parts, key=counts.__getitem__)
        return {anchor: (title_entity_pattern((anchor,), ignore_spacing=True), True)}
    return {}


def related_article_ids(conn, params: dict, terms: dict) -> list[int]:
    """표시 후보 한도를 적용하기 전에 필수어가 하나 이상 일치하는 기사 ID를 모은다."""
    scope, scope_values = article_filter(params, correlated_publisher=True)
    context, context_values = context_filter(params)
    short_names = short_person_names(params)
    if len(terms) > 1 and not short_names:
        # 제목을 한 번 읽고, 기간·언론사·중복·맥락 조건은 합쳐진 후보에 적용한다.
        titles = [title_scan_check(word, pattern) for word, (pattern, _) in terms.items()]
        title_sql = ' OR '.join(sql for sql, _ in titles)
        title_values = tuple(value for _, values in titles for value in values)
        calendar, calendar_values = article_filter({key: params.get(key) for key in ('date_from', 'date_to')})
        keywords = sorted({form for word, (_, required) in terms.items() if not required
                           for form in core_keyword_forms(word)})
        rows = conn.execute(f'''
            WITH possible AS MATERIALIZED (
                SELECT a.id FROM articles a WHERE ({title_sql}) {calendar}
                UNION SELECT article_id FROM article_keywords WHERE keyword=ANY(%s::text[])
            )
            SELECT p.id FROM possible p CROSS JOIN LATERAL (
                SELECT 1 FROM articles a WHERE a.id=p.id {scope} {context} OFFSET 0
            ) eligible
        ''', (*title_values, *calendar_values, keywords, *scope_values, *context_values)).fetchall()
        return [row['id'] for row in rows]
    sources, values = [], []
    for word, (pattern, title_required) in terms.items():
        sources.append(f'SELECT a.id FROM articles a WHERE a.title ~* %s {scope} {context}')
        values.extend((pattern, *scope_values, *context_values))
        if not title_required:
            sources.append(f'''SELECT a.id FROM article_keywords k JOIN articles a ON a.id=k.article_id
                               WHERE k.keyword=ANY(%s::text[]) {scope} {context}''')
            values.extend((core_keyword_forms(word), *scope_values, *context_values))
    join = ' INTERSECT ' if short_names else ' UNION '
    return [row['id'] for row in conn.execute(join.join(sources), tuple(values)).fetchall()]


def core_article_query(params: dict, vector: str | None, terms: dict[str, tuple[str, bool]], *,
                       share_keyword_candidates: bool = False) -> tuple[str, tuple]:
    """제목·키워드 조건을 만족하는 전체 기사. 후보 제한 전에 한 번 판정한다."""
    conditions, scope = article_filter(params, correlated_publisher=True)
    context, context_values = context_filter(params)
    focus = topic_title_focus(params['q'])
    exclusions = [focus[2]] if focus and focus[2] != r'(?!)' else []
    if '솔로' in terms and '크리스마스' in terms:
        exclusions.append(r'나는\s*솔로|솔로\s*데뷔|싱글\s*(?:앨범|음반)')
    if '이적' in terms:
        people = core_person_names(params, fixed_person_name(params['q']))
        exclusions.extend(rf'(?:{pattern})\s*(?:의\s*)?후계자'
                          for word, (pattern, _) in terms.items() if word in people)
    if exclusions:
        context += ' AND a.title !~* %s'
        context_values += ('|'.join(exclusions),)
    if len(terms) > 1 and all(required for _, required in terms.values()):
        title_checks = ' AND '.join('a.title ~* %s' for _ in terms)
        return (f'SELECT a.id, true AS title_core FROM articles a WHERE {title_checks} {conditions} {context}',
                (*(pattern for pattern, _ in terms.values()), *scope, *context_values))
    # 제목은 진입 키워드 하나로 조회하고 나머지 공백 무시 비교는 후보 기사에만 적용한다.
    anchor, (anchor_pattern, title_required) = min(
        terms.items(), key=lambda item: not item[1][1] and item[0] not in params['keywords'])
    source_scope, source_scope_values = article_filter(params, correlated_publisher=True, dedupe=False)
    title_scan, title_scan_values = title_scan_check(anchor, anchor_pattern)
    if len(terms) == 1:
        # 제목과 키워드의 일치 여부는 각 조회에서 이미 확인하므로 합친 뒤 다시 검사하지 않는다.
        source = f'SELECT a.id, true AS title_core FROM articles a WHERE {title_scan} {source_scope} {context}'
        source_values = (*title_scan_values, *source_scope_values, *context_values)
        if not title_required:
            if source_scope or context:
                source += f''' UNION ALL SELECT k.article_id, false FROM article_keywords k
                               JOIN articles a ON a.id=k.article_id
                               WHERE k.keyword=ANY(%s::text[]) {source_scope} {context}'''
                source_values += (core_keyword_forms(anchor), *source_scope_values, *context_values)
            else:
                source += ' UNION ALL SELECT article_id, false FROM article_keywords WHERE keyword=ANY(%s::text[])'
                source_values += (core_keyword_forms(anchor),)
        unique, unique_values = duplicate_filter(params, 'id')
        return f'''
            WITH possible AS MATERIALIZED ({source})
            SELECT id, bool_or(title_core) AS title_core FROM possible WHERE TRUE {unique} GROUP BY id
        ''', (*source_values, *unique_values)
    source = f'SELECT a.id, true AS anchor_title FROM articles a WHERE {title_scan} {source_scope}'
    source_values = (*title_scan_values, *source_scope_values)
    if not title_required:
        source += ''' UNION ALL SELECT article_id, false FROM article_keywords WHERE keyword=ANY(%s::text[])'''
        source_values += (core_keyword_forms(anchor),)
    matches, match_values = [], []
    keyword_join, keyword_values = '', ()
    other_terms = {word: term for word, term in terms.items() if word != anchor}
    for word, (pattern, required) in other_terms.items():
        if len(other_terms) == 1 and not required and not source_scope:
            # 넓은 검색의 두 번째 키워드는 후보마다 인덱스를 탐색하지 않고 한 번에 조인한다.
            keyword_join = '''LEFT JOIN (
                SELECT DISTINCT article_id FROM article_keywords WHERE keyword=ANY(%s::text[])
            ) second_term ON second_term.article_id=p.id'''
            keyword_values = (core_keyword_forms(word),)
            predicate, values = '(a.title ~* %s OR second_term.article_id IS NOT NULL)', (pattern,)
        else:
            predicate, values = article_term_check(word, pattern, required)
        matches.append(predicate)
        match_values.extend(values)
    title_checks = ' AND '.join('a.title ~* %s' for _ in other_terms)
    # 진입 근거를 재검사하지 않고 기사 ID 순서로 나머지 제목·키워드를 조회한다.
    candidate_join = '''CROSS JOIN LATERAL (
        SELECT id, title, published_date FROM articles WHERE id=p.id OFFSET 0
    ) a'''
    output, output_values = 'SELECT id, title_core FROM matched', ()
    if share_keyword_candidates:
        unique, output_values = duplicate_filter(params, 'p.id')
        output = f'''SELECT p.id, matched.title_core FROM possible p
                     LEFT JOIN matched USING (id) WHERE TRUE {unique}'''
    return f'''
        WITH possible AS MATERIALIZED (
            SELECT id, bool_or(anchor_title) AS anchor_title FROM ({source}) source GROUP BY id ORDER BY id
        ), matched AS MATERIALIZED (
            SELECT a.id, (p.anchor_title AND {title_checks}) AS title_core
            FROM possible p {keyword_join} {candidate_join}
            WHERE {' AND '.join(matches)} {conditions} {context}
        )
        {output}
    ''', (*source_values, *(pattern for pattern, _ in other_terms.values()), *keyword_values, *match_values,
          *scope, *context_values, *output_values)


def find_core_articles(conn, params: dict, vector: str | None, terms: dict) -> dict[int, bool]:
    if not terms:
        return {}
    sql, values = core_article_query(params, vector, terms)
    return {row['id']: row['title_core'] for row in conn.execute(sql, values).fetchall()}


def can_share_core_keyword_candidates(params: dict, terms: dict) -> bool:
    if (len(terms) < 2 or len(params['keywords']) != 1 or any(required for _, required in terms.values())
            or any(params.get(key) for key in ('date_from', 'date_to', 'publisher'))):
        return False
    keyword, = params['keywords']
    # 이 경우 진입 키워드의 제목 패턴과 별칭 집합이 keyword_source와 같다.
    return (keyword in COMPANY_ALIASES and re.fullmatch(r'[가-힣]+', keyword) is not None
            and terms.get(keyword) == (term_title_pattern(keyword), False)
            and all(alias.lower() == alias for alias in core_keyword_forms(keyword)))


def date_window_absence(conn, params: dict, terms: dict, *, has_related: bool = False) -> dict:
    start, end = (params.get(key) for key in ('date_from', 'date_to'))
    if not terms or not (start or end):
        return {}
    unbounded = {**params, 'date_from': None, 'date_to': None}
    unique = ''
    if unbounded.pop('_duplicate_article_ids', None) is not None:
        # 기간 밖에서 찾은 기사만 같은 날의 원본과 비교한다. 키워드가 다른 원본도 최저 ID 판정에 포함한다.
        unique = f'''AND NOT EXISTS (
            SELECT 1 FROM articles older
            LEFT JOIN article_sources older_source ON older_source.article_id=older.id
            WHERE older.published_date=a.published_date AND older.id<a.id
              AND {normalized_article_title_sql('older.title')} COLLATE "C"
                  = {normalized_article_title_sql('a.title')} COLLATE "C"
              AND older_source.publisher IS NOT DISTINCT FROM s.publisher
            OFFSET 0
        )'''
    sql, values = core_article_query(unbounded, None, terms)
    nearest = conn.execute(f'''
        SELECT a.id, a.title, a.published_date, a.url, s.publisher
        FROM ({sql}) matched CROSS JOIN LATERAL (
            SELECT id, title, published_date, url FROM articles WHERE id=matched.id OFFSET 0
        ) a
        LEFT JOIN LATERAL (
            SELECT publisher FROM article_sources WHERE article_id=a.id OFFSET 0
        ) s ON TRUE
        CROSS JOIN (SELECT %s::date AS first_day, %s::date AS last_day) bounds
        WHERE (a.published_date < bounds.first_day OR a.published_date > bounds.last_day) {unique}
        ORDER BY CASE WHEN a.published_date < bounds.first_day THEN bounds.first_day - a.published_date
                      ELSE a.published_date - bounds.last_day END,
                 a.published_date DESC, a.id DESC LIMIT 5
    ''', (*values, start, end)).fetchall()
    if not nearest and has_related:
        return {}
    if start and end and start[:4] == end[:4]:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        period = f'{first.month}/{first.day}~{last.month}/{last.day}'
    elif start and end:
        period = f'{start}~{end}'
    else:
        period = f'{start} 이후' if start else f'{end} 이전'
    subject = ' · '.join(terms)
    message = f"선택한 기간({period})에는 '{subject}' 기사가 없습니다."
    if nearest:
        message += f" 가장 가까운 기사: {nearest[0]['published_date']}"
    return {'data_absence_message': message, 'nearest_events': nearest}


def candidate_query(params: dict, vector: str | None, candidate_ids=None, *, core=None, lexical=False,
                    partial_terms=None) -> tuple[str, tuple]:
    prefix = ""
    extra = ""
    select_values = ()
    scope, scope_values = article_filter(params, dedupe=False)
    conditions, filter_values = article_filter(params)
    if params['mode'] == 'digest':
        return f"""
            WITH scoped AS MATERIALIZED (
                SELECT a.id, a.published_date, g.group_id, g.is_representative, s.publisher,
                       g.group_id IS NOT NULL AS grouped, COALESCE(g.group_id, a.id) AS event_id,
                       {SHORT_TITLE_SQL} AS short_title, a.title ~ %s AS routine_title
                FROM articles a LEFT JOIN article_groups g ON g.article_id=a.id
                LEFT JOIN LATERAL (
                    SELECT publisher FROM article_sources WHERE article_id=a.id OFFSET 0
                ) s ON TRUE
                WHERE TRUE {conditions}
            ), coverage AS (
                SELECT grouped, event_id, count(DISTINCT NULLIF(publisher, '')) AS publisher_count,
                       count(*) AS article_count, max(published_date) AS latest_match
                FROM scoped GROUP BY grouped, event_id
            ), ranked AS (
                SELECT *, row_number() OVER (PARTITION BY grouped, event_id ORDER BY routine_title, short_title,
                                          is_representative DESC NULLS LAST, published_date DESC, id DESC) AS position
                FROM scoped
            )
            SELECT id, published_date, group_id, NULL::double precision AS score,
                   short_title, routine_title, latest_match, publisher
            FROM ranked JOIN coverage USING (grouped, event_id) WHERE position=1
            ORDER BY routine_title, publisher_count DESC, article_count DESC, short_title, latest_match DESC, id DESC LIMIT %s
        """, (ROUTINE_TITLE, *filter_values, VECTOR_CANDIDATES)
    excluded = params.get('excluded_categories')
    category_sql, category_values = category_flag(excluded) if excluded else category_mismatch_flag(params)
    category_column = 'category_excluded' if excluded else 'category_mismatch'
    aspect = topic_aspect_pattern(params.get('q', ''))
    additional = (additional_title_pattern(params)
                  if params['mode'] == 'combined' and 'supporting_terms' not in params else '')
    support = [article_term_check(word, supporting_title_pattern(word), False)
               for word in params.get('supporting_terms', ())]
    support_column = ', (' + ' + '.join(f'{sql}::int' for sql, _ in support) + ') AS support_matches' if support else ''
    support_values = tuple(value for _, values in support for value in values)
    if partial_terms is None:
        partial_terms = related_terms(params, core_terms(params))
    partial = [article_term_check(word, pattern, title_required)
               for word, (pattern, title_required) in partial_terms.items()]
    match_join = ' AND ' if short_person_names(params) else ' OR '
    partial_column = ', (' + match_join.join(sql for sql, _ in partial) + ') AS partial_match' if partial else ''
    partial_values = tuple(value for _, values in partial for value in values)
    if params["mode"] == "keyword":
        source, values = (keyword_source(params["keywords"]) if candidate_ids is None
                          else ("SELECT article_id FROM unnest(%s::bigint[]) c(article_id)", (candidate_ids,)))
        score = "NULL::double precision"
        if national_team_sports(params.get('q', ''), params['keywords']):
            context_flag, select_values = required_title_flag(params)
            extra = f', COALESCE({context_flag}, FALSE) AS person_context_match'
    elif params["mode"] == "combined":
        # OFFSET 0과 MATERIALIZED로 후보 ID의 PK 조회를 먼저 확정한다.
        # 외부 CTE의 거리 정렬에는 HNSW를 사용할 수 없어 후보 전체의 정확 거리다.
        focus = topic_title_focus(params['q'])
        literal = literal_title_focus(params)
        signals = focus or literal or title_signals(params)
        prioritize_title = bool(signals and (aspect or focus or literal))
        context_flag, context_values = required_title_flag(params)
        if signals or category_sql or context_flag or core or support or lexical:
            count_sql = 'count(*) OVER ()'
            count_values = ()
            if core and not (excluded or context_flag or support):
                # 단일 목록에서는 핵심 여부와 제목 근거가 거리보다 앞선다.
                preferred = [article_id for article_id in candidate_ids if core.get(article_id)]
                if len(preferred) < VECTOR_CANDIDATES:
                    preferred = [article_id for article_id in candidate_ids if article_id in core]
                if VECTOR_CANDIDATES <= len(preferred) < len(candidate_ids):
                    # 제외한 기사를 포함해 기존 정확 순위 후보 수를 유지한다.
                    prefix = '''candidate_total AS (
                        SELECT count(*) AS total FROM unnest(%s::bigint[]) c(article_id)
                        JOIN articles a ON a.id=c.article_id
                    ), '''
                    count_sql = '(SELECT total FROM candidate_total)'
                    count_values = (candidate_ids,)
                    candidate_ids = preferred
            title_columns = f', {TITLE_SIGNALS_SQL}' if signals else ''
            category_select = f', {category_sql} AS {category_column}' if category_sql else ''
            context_select = f', COALESCE({context_flag}, FALSE) AS person_context_match' if context_flag else ''
            core_join = ''
            core_select = ''
            core_values = ()
            if core:
                prefix += '''core_articles AS MATERIALIZED (
                    SELECT * FROM unnest(%s::bigint[], %s::boolean[]) AS wanted(article_id, title_core)
                ), '''
                core_join = 'LEFT JOIN core_articles core ON core.article_id=a.id'
                core_select = ', core.article_id IS NOT NULL AS core_match, COALESCE(core.title_core, FALSE) AS title_core'
                core_values = (list(core), list(core.values()))
            prefix += f"""eligible AS MATERIALIZED (
                SELECT a.id AS article_id, 1-(e.embedding <=> %s::vector) AS score
                    {title_columns}{category_select}{context_select}{core_select}{support_column}
                FROM unnest(%s::bigint[]) c(article_id) JOIN articles a ON a.id=c.article_id
                {core_join}
                LEFT JOIN LATERAL (
                    SELECT embedding FROM article_embeddings WHERE article_id=a.id OFFSET 0
                ) e ON TRUE
                {'WHERE e.embedding IS NOT NULL' if not (signals or core or lexical) else ''}
            ), """
            rank = TITLE_RANK_SQL if signals else ('COALESCE(score, 0.78)' if core or lexical else 'score')
            if focus:
                rank += ' + 0.32 * focus_match::int'
            if category_sql and not excluded:
                rank = category_rank_sql(rank)
            ranking = f'SELECT *, {rank} AS rank_score, {count_sql} AS ranked_candidate_count FROM eligible'
            order = ('context_match DESC, ' if prioritize_title else '') + 'rank_score DESC, article_id DESC'
            if core:
                order = 'core_match DESC, ' + ('support_matches DESC, ' if support else '') + 'title_core DESC, ' + order
            if excluded or context_flag:
                partition = 'person_context_match'
                if excluded:
                    partition = ('category_excluded, (NOT category_excluded AND person_context_match)'
                                 if context_flag else 'category_excluded')
                source = f"""SELECT * FROM (
                    SELECT *, row_number() OVER (PARTITION BY {partition} ORDER BY {order}) AS category_rank
                    FROM ({ranking}) ranked
                ) partitioned WHERE category_rank <= %s"""
            else:
                source = f'{ranking} ORDER BY {order} LIMIT %s'
            score = "c.score"
            extra = ", c.ranked_candidate_count, c.rank_score"
            if signals:
                extra += ", c.name_match, c.context_match, c.wrong_context"
                if prioritize_title:
                    extra += ', c.context_match AS aspect_match'
            if category_sql:
                extra += f', c.{category_column}'
            if context_flag:
                extra += ', c.person_context_match'
            if support:
                extra += ', c.support_matches'
            if excluded and signals:
                extra += """, EXISTS (SELECT 1 FROM article_categories
                             WHERE article_id=a.id AND btrim(major)='미분류') AS category_unclassified"""
            values = (*count_values, *core_values, vector, *(signals or ()), *category_values, *context_values,
                      *support_values, sorted(candidate_ids), VECTOR_CANDIDATES)
        else:
            prefix = """eligible AS MATERIALIZED (
            SELECT e.article_id, e.embedding FROM unnest(%s::bigint[]) c(article_id)
            CROSS JOIN LATERAL (
                SELECT article_id, embedding FROM article_embeddings
                WHERE article_id=c.article_id OFFSET 0
            ) e
            ), """
            source = """SELECT e.article_id, e.embedding <=> %s::vector AS distance,
                           count(*) OVER () AS ranked_candidate_count
                        FROM eligible e ORDER BY e.embedding <=> %s::vector, e.article_id DESC LIMIT %s"""
            score = "1-c.distance"
            extra = ", c.ranked_candidate_count"
            values = (candidate_ids, vector, vector, VECTOR_CANDIDATES)
    else:
        # 조건이 있으면 해당 집합 안에서 정확 거리 평가한다. HNSW 후필터로 후보가 고갈되는 것을 막는다.
        # 조건이 없으면 기존 HNSW 거리 정렬/LIMIT을 그대로 사용한다.
        if scope:
            prefix = f'''eligible AS MATERIALIZED (
                SELECT e.article_id, e.embedding FROM articles a
                JOIN article_embeddings e ON e.article_id=a.id WHERE TRUE {conditions}
            ), '''
        relation = 'eligible' if scope else 'article_embeddings'
        unique, unique_values = duplicate_filter(params, 'article_id') if not scope else ('', ())
        predicate = f' WHERE TRUE{unique}' if unique else ''
        source = f"""SELECT article_id, embedding <=> %s::vector AS distance
                    FROM {relation}{predicate} ORDER BY distance LIMIT %s"""
        score = "1-c.distance"
        values = (*filter_values, vector, VECTOR_CANDIDATES) if scope else (vector, *unique_values, VECTOR_CANDIDATES)
        if category_sql:
            extra = f', {category_sql} AS {category_column}'
            values += category_values
        if aspect:
            extra += ', a.title ~* %s AS aspect_match'
            select_values = (aspect,)
    if params['mode'] != 'keyword' or candidate_ids is not None:
        conditions, filter_values = scope, scope_values
    return f"""
        WITH {prefix}candidates AS MATERIALIZED ({source})
        SELECT a.id, a.published_date, g.group_id, {score} AS score{extra}{partial_column},
               CASE WHEN g.group_id IS NULL THEN
                   (SELECT s.publisher FROM article_sources s WHERE s.article_id=a.id)
               END AS publisher,
               {SHORT_TITLE_SQL} AS short_title, a.title ~ %s AS routine_title
               {', a.title ~* %s AS additional_match' if additional else ''}
        FROM candidates c JOIN articles a ON a.id=c.article_id
        LEFT JOIN LATERAL (
            SELECT group_id FROM article_groups WHERE article_id=a.id OFFSET 0
        ) g ON TRUE
        WHERE TRUE {conditions}
    """, (*values, *select_values, *partial_values, ROUTINE_TITLE, *((additional,) if additional else ()), *filter_values)


def result_min_cos() -> float:
    try:
        value = float(os.environ.get("RESULT_MIN_COS", RESULT_MIN_COS))
    except (TypeError, ValueError):
        return RESULT_MIN_COS
    return value if math.isfinite(value) and -1 <= value <= 1 else RESULT_MIN_COS


def sort_result_events(events: list[dict], mode: str, *, similarity: bool = False,
                       importance_tiebreak: bool = True) -> list[dict]:
    if mode == 'digest':
        return sorted(events, key=lambda e: (not e['matches'][0]['routine_title'], event_coverage(e)['importance'],
                      not e['matches'][0]['short_title'], e['latest_match'], e['group_id']), reverse=True)
    def importance(event):
        return event_coverage(event)['importance']

    def rank(event):
        score = event.get("rank_score", event["score"])
        return score if score is not None else -math.inf

    def relevance_order(event):
        score = rank(event)
        return event.get('aspect_match', False), score, importance(event) if importance_tiebreak and score != -math.inf else 0

    events = sorted(events, key=lambda e: e['group_id'])
    events.sort(key=importance if importance_tiebreak else lambda e: e['article_count'], reverse=True)
    if mode in ("topic", "combined"):
        events.sort(key=rank, reverse=True)
    events.sort(key=lambda e: e["latest_match"], reverse=True)
    if similarity and mode in ("topic", "combined"):
        events.sort(key=relevance_order, reverse=True)
    return sorted(events, key=lambda event: event.get('tier') != 'core')


def select_monthly_result_events(events: list[dict], monthly: list[dict], limit: int) -> list[dict]:
    """사건 시작 월별로 자리를 배분하고 입력 목록의 정렬 순서를 유지한다."""
    if len(events) <= limit:
        return events
    by_month: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        by_month.setdefault(str(event['date_from'])[:7], []).append(index)
    counts = {row['month']: row['count'] for row in monthly}
    weights = {month: counts.get(month, 0) for month in by_month}
    if not any(weights.values()):
        weights = {month: sum(events[index]['matched_count'] for index in indexes)
                   for month, indexes in by_month.items()}

    months = sorted(by_month, key=lambda month: (weights[month], month), reverse=True)[:limit]
    slots = dict.fromkeys(months, 1)
    # 가능한 달에 한 자리씩 준 뒤, 다음 자리당 기사 수가 가장 큰 달에 배분한다.
    for _ in range(limit - len(months)):
        month = max((month for month in months if slots[month] < len(by_month[month])),
                    key=lambda month: (weights[month] / (slots[month] + 1), weights[month], month))
        slots[month] += 1

    def within_month_rank(index):
        event = events[index]
        return event_coverage(event)['importance'], event['matched_count'], event['latest_match']

    selected = sorted(index for month in months for index in
                      sorted(by_month[month], key=within_month_rank, reverse=True)[:slots[month]])
    return [events[index] for index in selected]


def fold_events(rows: list[dict], mode: str) -> list[dict]:
    groups: dict[tuple[bool, int], list] = {}
    for row in rows:
        grouped = row["group_id"] is not None
        groups.setdefault((grouped, row["group_id"] if grouped else row["id"]), []).append(row)
    events = []
    for (grouped, group_id), matches in groups.items():
        matches.sort(key=lambda r: (
            r.get('tier') == 'core', r.get('support_matches', 0), r.get('title_core', False),
            r.get('aspect_match', False),
            r.get("rank_score", r["score"] if r["score"] is not None else -math.inf)
            if mode in ("topic", "combined") else r["published_date"], r["id"]
        ), reverse=True)
        events.append({"group_id": group_id, "score": matches[0]["score"],
                       "latest_match": max(r.get("latest_match", r["published_date"]) for r in matches),
                       "grouped": grouped, "matched_count": len(matches), "matches": matches[:3],
                       **({'tier': matches[0]['tier']} if 'tier' in matches[0] else {}),
                       **({'aspect_match': matches[0]['aspect_match']} if 'aspect_match' in matches[0] else {}),
                       **({'rank_score': matches[0]['rank_score']} if 'rank_score' in matches[0] else {})})
    return events


def stats_aspects(params: dict) -> dict[str, str]:
    """필수어와 구분되는 관점마다 집계에 쓸 제목·키워드 단서를 만든다."""
    if not params['keywords']:
        return {}
    topic = params.get('q', '')
    cues = topic_aspects(topic)
    person = fixed_person_name(topic)
    aspects = {}
    for term in timeline_aspect_terms({**params, 'q': topic}):
        if person and term.casefold() in PERSON_CONTEXT_WORDS:
            continue
        forms = web_word_forms(term)
        registered = next((word for word in cues if word in forms), None)
        if registered:
            aspects[registered] = cues[registered]
        else:
            aspects[term] = title_entity_pattern((term,))
    return aspects


def stats_query(keywords: list[str], candidate_ids=None, params: dict | None = None, *,
                aspects: dict[str, str] | None = None, core_scope: bool = False,
                monthly_title_ids: list[int] | None = None, excluded_keywords: list[str] | None = None) -> tuple[str, tuple]:
    """같은 집합 전체 대비 월별 비중 상승을 한 SQL에서 계산한다."""
    params = {'keywords': keywords, **(params or {})}
    if aspects is None:
        aspects = stats_aspects(params)
    source, values = (keyword_source(keywords, params.get('q', '')) if keywords else
                      ("SELECT article_id FROM unnest(%s::bigint[]) c(article_id)", (candidate_ids,)))
    conditions, filter_values = article_filter(params, correlated_publisher=bool(keywords))
    context_conditions, context_values = context_filter(params)
    if core_scope:
        # 이미 판정된 기사 ID 또는 기간 내 뉴스 전체를 집계한다.
        if params['mode'] == 'digest':
            source, values = f'SELECT a.id AS article_id FROM articles a WHERE TRUE {conditions}', filter_values
        else:
            source, values = 'SELECT article_id FROM unnest(%s::bigint[]) c(article_id)', (candidate_ids,)
        conditions, filter_values = '', ()
        context_conditions, context_values = '', ()
        aspects, keywords = {}, []
    aspect_values = []
    for word, pattern in aspects.items():
        keyword_exclusion = ''
        if word == '실적':
            pattern = EARNINGS_ASPECT_CUES
            context_conditions += ' AND a.title !~* %s'
            aspect_values.append(EARNINGS_ADJACENT)
            keyword_exclusion = ' AND ak.keyword !~* %s'
        # 관점어로 키워드 테이블 전체를 스캔하지 않고 해당 기사 ID의 인덱스 범위만 본다.
        context_conditions += f""" AND (a.title ~* %s OR EXISTS (
                SELECT 1 FROM article_keywords ak WHERE ak.article_id=a.id
                AND ak.keyword ~* %s{keyword_exclusion} OFFSET 0
            ))"""
        aspect_values.extend((pattern, pattern))
        if keyword_exclusion:
            aspect_values.append(EARNINGS_ADJACENT)
    # 검색 후보에는 관점·맥락 집계와 별도로 날짜·언론사로 고른 ID를 반환한다.
    context_cte = ''
    relation = 'dated'
    if context_conditions:
        relation = 'context_matched'
        context_cte = f"""context_matched AS MATERIALIZED (
            SELECT a.article_id, a.month{', a.published_date, a.title' if monthly_title_ids is not None else ''} FROM dated a
            WHERE TRUE {context_conditions}
        ), """
    representatives = ''
    representative_values = ()
    if monthly_title_ids is not None:
        signals, _ = timeline_title_signals(params)
        if signals:
            context_hit = '(d.title ~* %s AND d.title ~* %s AND d.title !~* %s)'
            representative_values = signals[:3]
        else:
            context_hit = 'FALSE'
        # 차트 범위와 월 안에서 묶인 기사만 집계하고 제목 근거로 대표 보도를 고른다.
        representatives = f""", (
            WITH evidence AS (
                SELECT d.article_id, d.month, d.published_date,
                       COALESCE(g.group_id, d.article_id) AS group_id, g.group_id IS NOT NULL AS grouped,
                       s.publisher, {context_hit} AS context_hit, title_hit.id IS NOT NULL AS title_hit
                FROM {relation} d
                LEFT JOIN unnest(%s::bigint[]) AS title_hit(id) ON title_hit.id=d.article_id
                LEFT JOIN article_groups g ON g.article_id=d.article_id
                LEFT JOIN article_sources s ON s.article_id=d.article_id
                WHERE d.month IS NOT NULL
            ), coverage AS (
                SELECT month, group_id, grouped, min(published_date) AS date_from, max(published_date) AS date_to,
                       (array_agg(article_id ORDER BY context_hit DESC, title_hit DESC,
                                  published_date DESC, article_id DESC))[1] AS representative_id,
                       array_agg(article_id ORDER BY article_id) AS article_ids,
                       array_agg(DISTINCT publisher ORDER BY publisher) FILTER (WHERE publisher IS NOT NULL) AS publishers,
                       count(DISTINCT publisher) AS publisher_count, count(*) AS article_count,
                       bool_or(context_hit) AS context_hit, bool_or(title_hit) AS title_hit
                FROM evidence GROUP BY month, group_id, grouped
            ), ranked_events AS (
                SELECT *, row_number() OVER (
                    PARTITION BY month ORDER BY context_hit DESC, (publisher_count > 1) DESC,
                        title_hit DESC, publisher_count DESC, article_count DESC, date_to DESC, representative_id DESC
                ) AS rank FROM coverage
            )
            SELECT COALESCE(jsonb_agg(to_jsonb(r) - 'rank' ORDER BY month, rank), '[]'::jsonb)
            FROM ranked_events r WHERE rank <= %s
        ) AS monthly_event_candidates"""
        # 선정 단계에서 이미 사용했거나 관련성 판정을 통과하지 못한 묶음을 건너뛸 여유를 둔다.
        representative_values = (*representative_values, monthly_title_ids, TIMELINE_CANDIDATES + TIMELINE_MONTH_LIMIT)
    title_column = (', a.id' if context_conditions else '') + (', a.title' if context_conditions or representatives else '')
    if excluded_keywords is None:
        excluded_keywords = sorted(KEYWORD_STOPWORDS | aspects.keys() | {
            alias for k in keywords for alias in ENTITY_ALIASES.get(k, (k,))
        })
    return f"""
        WITH matched AS MATERIALIZED ({source}),
        dated AS MATERIALIZED (
            SELECT m.article_id, date_trunc('month', a.published_date)::date AS month
                   {', a.published_date' if representatives else ''}
                   {title_column}
            FROM matched m CROSS JOIN LATERAL (
                SELECT id, published_date{', title' if context_conditions or representatives else ''}
                FROM articles WHERE id=m.article_id OFFSET 0
            ) a
            WHERE TRUE {conditions}
        ), {context_cte}monthly_counts AS (
            SELECT month, count(*) AS count FROM {relation} GROUP BY month
        ), months AS (
            SELECT generate_series(LEAST('2026-01-01'::date, (SELECT min(month) FROM monthly_counts)),
                GREATEST('2026-01-01'::date, date_trunc('month', (
                    SELECT published_date FROM articles ORDER BY published_date DESC LIMIT 1
                ))::date), '1 month'::interval)::date AS month
        ), keyword_sums AS (
            SELECT d.month, k.keyword COLLATE "C" AS keyword,
                   sum(k.mention_count) AS mention_sum, count(*) AS article_count
            FROM {relation} d CROSS JOIN LATERAL (
                SELECT keyword, mention_count FROM article_keywords
                WHERE article_id=d.article_id OFFSET 0
            ) k
            GROUP BY d.month, k.keyword COLLATE "C"
        ), shares AS (
            SELECT *, mention_sum::double precision
                / NULLIF(sum(mention_sum) OVER (PARTITION BY month), 0)
                / NULLIF(sum(mention_sum) OVER (PARTITION BY keyword)
                    / NULLIF(sum(mention_sum) OVER (), 0), 0) AS lift
            FROM keyword_sums
        ), excluded_keywords AS (SELECT %s::text[] AS words), ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY month ORDER BY mention_sum * ln(lift) DESC, keyword COLLATE "C"
            ) AS rank FROM shares CROSS JOIN excluded_keywords
            WHERE article_count >= 3 AND lift > 1
              AND (SELECT count(*) FROM monthly_counts) > 1
              AND NOT (keyword=ANY(words))
        ), top_keywords AS (
            SELECT month, jsonb_agg(jsonb_build_object('keyword', keyword,
                       'mention_sum', mention_sum, 'article_count', article_count,
                       'lift', lift) ORDER BY rank) AS items
            FROM ranked WHERE rank <= 4 GROUP BY month
        ), frequency_ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY month ORDER BY mention_sum DESC, keyword COLLATE "C"
            ) AS rank FROM keyword_sums CROSS JOIN excluded_keywords
            WHERE NOT (keyword=ANY(words))
              AND NOT EXISTS (SELECT 1 FROM top_keywords t WHERE t.month=keyword_sums.month)
        ), frequency_keywords AS (
            SELECT month, jsonb_agg(jsonb_build_object('keyword', keyword,
                       'mention_sum', mention_sum, 'article_count', article_count,
                       'lift', NULL) ORDER BY rank) AS items
            FROM frequency_ranked WHERE rank <= 4 GROUP BY month
        )
        SELECT ARRAY(SELECT article_id FROM dated) AS article_ids,
               (SELECT jsonb_agg(jsonb_build_object('month', to_char(m.month, 'YYYY-MM'),
                       'count', COALESCE(c.count, 0), 'top_keywords', COALESCE(k.items, f.items, '[]'::jsonb))
                       ORDER BY m.month)
                FROM months m LEFT JOIN monthly_counts c USING (month)
                LEFT JOIN top_keywords k USING (month)
                LEFT JOIN frequency_keywords f USING (month)) AS monthly{representatives}
    """, (*values, *filter_values, *context_values, *aspect_values, excluded_keywords, *representative_values)


def collect_core_stats(conn, params: dict, core: dict[int, bool], terms: dict, *, related_ids: list[int] | None = None,
                       cover_months: bool = False) -> dict:
    # 기사와 기사 키워드의 ID 인덱스를 순서대로 읽어 빈도가 높은 검색의 무작위 접근을 줄인다.
    ids = sorted(related_ids if related_ids is not None else core)
    title_ids = [article_id for article_id in ids if core.get(article_id)] if cover_months else None
    # 표시용 키워드에서 질문에 이미 쓰인 핵심어와 별칭을 뺀다.
    exclusions = sorted(KEYWORD_STOPWORDS | {alias for word in terms for alias in core_keyword_forms(word)})
    sql, values = stats_query([], ids, params, core_scope=True, monthly_title_ids=title_ids, excluded_keywords=exclusions)
    row = conn.execute(sql, values).fetchone()
    basis = '핵심 결과 판정을 통과한 전체 기사'
    if related_ids is not None:
        basis = 'related'
    elif params['mode'] == 'digest':
        basis = '지정 기간·분류에 해당하는 전체 기사'
    return {'basis': basis, 'monthly': row['monthly'], **({'core_terms': list(terms)} if terms else {}),
            **({'monthly_event_candidates': row['monthly_event_candidates']} if cover_months else {})}


def monthly_counts_for_selection(conn, ids) -> list[dict]:
    # 사건 배분에는 기사 수만 필요하다. 차트의 빈 달과 키워드 순위는 별도 요청에서 계산한다.
    return conn.execute("""
        SELECT to_char(date_trunc('month', a.published_date), 'YYYY-MM') AS month, count(*) AS count
        FROM unnest(%s::bigint[]) matched(id)
        CROSS JOIN LATERAL (SELECT published_date FROM articles WHERE id=matched.id OFFSET 0) a
        GROUP BY 1
    """, (sorted(ids),)).fetchall()


def collect_stats(conn, params: dict, candidate_ids=None) -> tuple[dict, list[int]]:
    # 등록 구단의 종목·순위 표현은 기존 구단별 통계 기준을 보존한다.
    if TEAM_NAMES.intersection(params['keywords']) and not fixed_person_name(params.get('q', '')):
        params = {**params, 'excluded_categories': []}
    aspects = stats_aspects(params)
    sql, values = stats_query(params['keywords'], candidate_ids, params, aspects=aspects)
    row = conn.execute(sql, values).fetchone()
    categories = params.get('excluded_categories', [])
    title_flag, _ = person_title_flag(params)
    sports = national_team_sports(params.get('q', ''), params['keywords'])
    basis = ("필수 키워드 모두 일치한 전체 기사 · 주제(q)·임베딩 유무·결과 상한과 무관"
             if params["keywords"] else '기간·언론사 조건 내 주제 후보 기사 · 최대 200건' if article_filter(params, dedupe=False)[0]
             else "자연어 주제(q)의 HNSW 후보 기사 · 최대 200건")
    if aspects:
        basis = '필수 키워드 모두 일치한 기사 · 임베딩 유무·결과 상한과 무관'
    if params['mode'] == 'digest':
        basis = '기간 내 주요 사건별 대표 기사 · 최대 200건'
    if sports:
        basis = f'필수 키워드 모두 일치한 기사 · 국가대표 종목 맥락({", ".join(sports)}) 적용'
    elif categories:
        words = ', '.join(sorted(context_words(params['q'])))
        if params['keywords']:
            basis = '필수 키워드 모두 일치한 기사 · 임베딩 유무·결과 상한과 무관'
        basis += f' · 맥락({words}) 적용 · {", ".join(categories)} 분류 제외'
        if title_flag:
            basis += ' · 제목의 이름·직함 단서 일치'
    if aspects:
        basis += ' · 각 관점이 제목 또는 제공 키워드에 일치'
    if any(params.get(k) for k in ('date_from', 'date_to', 'publisher')):
        basis += ' · 지정 발행일·언론사 조건 적용'
    monthly = row['monthly']
    if (categories or sports) and not any(month['count'] for month in monthly):
        monthly = []
    return {'basis': basis, 'monthly': monthly, **({'aspects': list(aspects)} if aspects else {})}, row['article_ids']


def keyword_candidate_ids(conn, params: dict) -> list[int]:
    source, values = keyword_source(params['keywords'], params['q'], fast_titles=True)
    conditions, scope = article_filter(params, correlated_publisher=True, dedupe=False)
    unique, unique_values = duplicate_filter(params, 'c.article_id')
    if conditions:
        source = f'SELECT c.article_id FROM ({source}) c JOIN articles a ON a.id=c.article_id WHERE TRUE {conditions}'
        source += unique
    elif unique:
        source = f'SELECT c.article_id FROM ({source}) c WHERE TRUE {unique}'
    return [row['article_id'] for row in conn.execute(source, (*values, *scope, *unique_values)).fetchall()]


def article_evidence_column(alias: str = 'a') -> str:
    return f'ARRAY(SELECT k.keyword FROM article_keywords k WHERE k.article_id={alias}.id) AS evidence_keywords'


def article_match_reason(article: dict, terms: dict, supporting: list[str], *, basis: str = '') -> dict:
    keywords = set(article.pop('evidence_keywords', []))
    matches = {**terms, **{word: (supporting_title_pattern(word), False) for word in supporting if word not in terms}}
    title, keyword = [], []
    for word, (pattern, title_required) in matches.items():
        if re.search(pattern, article['title'], re.IGNORECASE):
            title.append(word)
        elif not title_required and keywords.intersection(core_keyword_forms(word)):
            keyword.append(word)
    return {'title': title, 'keywords': keyword, 'basis': '' if title or keyword else basis}


def event_articles(conn, events: list[dict], params: dict, vector: str | None = None, *, timeline: bool = False,
                   scoped_ids: list[int] | None = None) -> dict:
    groups = sorted({e['group_id'] for e in events if e['grouped']})
    singles = sorted({e['group_id'] for e in events if not e['grouped']} | set(scoped_ids or ()))
    if not groups and not singles:
        return {}
    conditions, filters = article_filter(params)
    context_conditions, context_values = (timeline_context_filter if timeline else context_filter)(params)
    score = '1-(e.embedding <=> %s::vector)' if vector else 'NULL::double precision'
    embedding = ('LEFT JOIN article_embeddings e ON e.article_id=a.id' if vector else '')
    mismatch, mismatch_values = category_mismatch_flag(params) if timeline and vector else ('', ())
    rank_order = f'{category_rank_sql(score, mismatch)} DESC NULLS LAST, ' if mismatch else ''
    rank_values = (vector, *mismatch_values) if mismatch else ()
    # 월별 보도 ID는 앞선 검색의 읽기 전용 스냅샷에서 이미 차트 조건을 통과했다.
    scoped = '(NOT m.grouped AND m.article_id=ANY(%s::bigint[])) OR ' if scoped_ids else ''
    rows = conn.execute(f'''
        WITH members AS (
            SELECT TRUE AS grouped, group_id, article_id FROM article_groups
            WHERE group_id=ANY(%s::bigint[])
            UNION ALL
            SELECT FALSE, id, id FROM unnest(%s::bigint[]) AS wanted(id)
        )
        SELECT m.grouped, m.group_id, a.id, a.published_date, a.title, a.url, s.publisher, {score} AS score,
               {article_evidence_column()}
        FROM members m JOIN articles a ON a.id=m.article_id
        LEFT JOIN article_sources s ON s.article_id=a.id {embedding}
        WHERE {scoped}(TRUE {conditions} {context_conditions})
        ORDER BY {rank_order}a.published_date DESC, a.id DESC
    ''', (groups, singles, *((vector,) if vector else ()), *((scoped_ids,) if scoped else ()),
          *filters, *context_values, *rank_values)).fetchall()
    by_event = {}
    terms = core_terms(params) if params.get('mode') != 'digest' else {}
    supporting = params.get('supporting_terms', [])
    for row in rows:
        key = (row.pop('grouped'), row.pop('group_id'))
        row['match_reason'] = article_match_reason(row, terms, supporting, basis='same_event')
        by_event.setdefault(key, []).append(row)
    return _json_values(by_event)


def followup_key(event: dict) -> tuple[int, str]:
    return event['representative']['id'], str(event['date_to'])


def followup_targets(title: str, keywords: list[str], params: dict) -> tuple[list[str], str]:
    def pattern(name):
        return title_entity_pattern(ENTITY_ALIASES.get(name, (name,)))

    def in_title(name):
        return re.search(pattern(name), title, re.IGNORECASE)

    topic = params.get('q', '')
    person = fixed_person_name(topic)
    if person and in_title(person):
        signals = title_signals({**params, 'keywords': [person]})
        return [person], signals[2] if signals[2] != r'(?!)' else ''

    entities = query_entities(title)
    teams = TEAM_NAMES.intersection(entities)
    if teams:
        # 구단 이름 뒤에 선수 이름이 나오는 제목에서는 구단보다 선수를 우선한다.
        team_pattern = '|'.join(pattern(team) for team in sorted(teams))
        player = re.search(rf'(?:{team_pattern})(?:\s+|\s*[,，]\s*)([가-힣]{{3,4}})(?![가-힣])',
                           title, re.IGNORECASE)
        if player and (player[1] in keywords or player[1] in params.get('keywords', [])) and not re.search(
                BASEBALL_CONTEXT, player[1]):
            return [player[1]], AUTO_CONTEXT

    excluded = ASK_STOPWORDS | MERGE_GENERAL_WORDS | CONTEXT_EXCLUSIONS.keys()
    required = [name for name in params.get('keywords', []) if name not in excluded and in_title(name)]
    targets = list(dict.fromkeys([*entities, *required]))
    if not targets:
        # 인물·기관 표기가 따로 없는 기사에서는 제목에 실제로 나온 대표 기사의 키워드로 좁힌다.
        targets = [name for name in keywords if name not in excluded
                   and len(name) >= 2 and name.isalpha() and in_title(name)][:3]
    return targets, AUTO_CONTEXT if teams else ''


def event_followups(conn, events: list[dict], params: dict) -> dict:
    representatives = {followup_key(event): event['representative']['title'] for event in events}
    ids = sorted({article_id for article_id, _ in representatives})
    keyword_rows = conn.execute('''
        SELECT article_id, array_agg(keyword ORDER BY mention_count DESC, keyword COLLATE "C") AS keywords
        FROM article_keywords WHERE article_id=ANY(%s::bigint[]) GROUP BY article_id
    ''', (ids,)).fetchall()
    keywords = {row['article_id']: row['keywords'] for row in keyword_rows}
    requests = []
    for (article_id, end_date), title in representatives.items():
        targets, wrong_context = followup_targets(title, keywords.get(article_id, []), params)
        if not targets:
            continue
        terms, patterns = [], []
        for target in targets:
            aliases = ENTITY_ALIASES.get(target, (target,))
            # 한글 일치와 별칭의 공통 접두사는 정규식 검사 전에 문자열로 거른다.
            if len(aliases) == 1 and re.fullmatch(r'[가-힣]+', target):
                terms.append(f'%{target}%')
            else:
                patterns.append(title_entity_pattern(aliases))
                prefix = re.match(r'[가-힣]+', os.path.commonprefix(aliases))
                if prefix:
                    terms.append(f'%{prefix[0]}%')
        requests.append(dict(article_id=article_id, end_date=end_date,
                             terms=terms, patterns=patterns, wrong_context=wrong_context))
    if not requests:
        return {}
    context_conditions, context_values = timeline_context_filter(params)
    title_filter = f'''
        CASE WHEN a.title LIKE ALL(w.terms) AND a.title ~* ALL(w.patterns)
                   AND (w.wrong_context = '' OR a.title !~* w.wrong_context)
            THEN TRUE {context_conditions} ELSE FALSE END
    '''
    mismatch, mismatch_values = category_mismatch_flag(params)
    unique, unique_values = duplicate_filter(params, 'a.id')
    scored = '1-(e.embedding <=> w.embedding) AS score' if mismatch else '1'
    rank_order = f'{category_rank_sql("scored.score", mismatch)} DESC NULLS LAST, ' if mismatch else ''
    similar = f'''
        CROSS JOIN LATERAL (
            SELECT {scored} FROM article_embeddings e
            WHERE e.article_id=a.id AND e.embedding <=> w.embedding <= 1.0 - %s
            OFFSET 0
        ) scored
    '''
    rows = conn.execute(f'''
        WITH wanted AS MATERIALIZED (
            SELECT input.*, end_date + %s AS last_date, rep.embedding
            FROM jsonb_to_recordset(%s::jsonb) AS input(
                article_id bigint, end_date date, terms text[], patterns text[], wrong_context text
            )
            CROSS JOIN LATERAL (
                SELECT embedding FROM article_embeddings WHERE article_id=input.article_id OFFSET 0
            ) rep
        ), donga_articles AS MATERIALIZED (
            SELECT a.id, a.published_date, a.title, a.url, s.publisher
            FROM article_sources s
            CROSS JOIN LATERAL (
                SELECT id, published_date, title, url FROM articles WHERE id=s.article_id OFFSET 0
            ) a
            WHERE s.publisher=%s
              AND a.published_date > (SELECT min(end_date) FROM wanted)
              AND a.published_date <= (SELECT max(last_date) FROM wanted)
        ), donga AS MATERIALIZED (
            SELECT w.article_id AS representative_id, w.end_date, later.*
            FROM wanted w
            CROSS JOIN LATERAL (
                SELECT a.*
                FROM (
                    SELECT a.* FROM donga_articles a
                    WHERE a.published_date > w.end_date AND a.published_date <= w.last_date
                      AND {title_filter}
                      {unique}
                    ORDER BY a.published_date, a.id
                ) a
                {similar}
                ORDER BY {rank_order}a.published_date, a.id
                LIMIT %s
            ) later
        ), missing AS (
            SELECT * FROM wanted w WHERE NOT EXISTS (
                SELECT 1 FROM donga d WHERE d.representative_id=w.article_id AND d.end_date=w.end_date
            )
        ), windows AS (
            SELECT terms, patterns, wrong_context, unnest(range_agg(daterange(end_date, last_date, '(]'))) AS days
            FROM missing GROUP BY terms, patterns, wrong_context
        ), title_matches AS MATERIALIZED (
            SELECT w.terms, w.patterns, w.wrong_context, a.*, s.publisher
            FROM windows w
            CROSS JOIN LATERAL (
                SELECT a.id, a.published_date, a.title, a.url
                FROM articles a
                WHERE a.published_date >= lower(w.days)
                  AND a.published_date < upper(w.days)
                  AND {title_filter}
                  {unique}
                OFFSET 0
            ) a
            JOIN article_sources s ON s.article_id=a.id AND s.publisher<>%s
        ), followups AS (
            SELECT * FROM donga
            UNION ALL
            SELECT w.article_id AS representative_id, w.end_date, later.*
            FROM missing w
            CROSS JOIN LATERAL (
                SELECT a.*
                FROM (
                    SELECT id, published_date, title, url, publisher FROM title_matches
                    WHERE terms=w.terms AND patterns=w.patterns AND wrong_context=w.wrong_context
                      AND published_date > w.end_date AND published_date <= w.last_date
                    ORDER BY published_date, id
                ) a
                {similar}
                ORDER BY {rank_order}a.published_date, a.id
                LIMIT %s
            ) later
        )
        SELECT later.*, {article_evidence_column('later')} FROM followups later
        ORDER BY representative_id, end_date, published_date, id
    ''', (FOLLOWUP_WINDOW_DAYS, json.dumps(requests, ensure_ascii=False), '동아일보', *context_values, *unique_values,
          FOLLOWUP_MIN_COS, *mismatch_values, FOLLOWUP_DONGA_LIMIT, *context_values, *unique_values, '동아일보',
          FOLLOWUP_MIN_COS, *mismatch_values, FOLLOWUP_OTHER_LIMIT)).fetchall()
    by_event = {}
    terms = core_terms(params) if params.get('mode') != 'digest' else {}
    supporting = params.get('supporting_terms', [])
    for row in rows:
        key = (row.pop('representative_id'), str(row.pop('end_date')))
        row['match_reason'] = article_match_reason(row, terms, supporting, basis='followup')
        by_event.setdefault(key, []).append(row)
    return _json_values(by_event)


def timeline_articles(items: list[dict], params: dict) -> tuple[dict, dict]:
    if not items:
        return {}, {}
    with psycopg.connect(**connection_options(Path(os.environ.get('BIGKINDS_ENV_FILE', str(DEFAULT_ENV)))),
                          autocommit=True, row_factory=dict_row, connect_timeout=10,
                          options='-c default_transaction_read_only=on') as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            conn.execute("SET LOCAL statement_timeout='10s'; SET LOCAL jit=off; SET LOCAL work_mem='32MB'")
            params = with_article_duplicates(conn, params, unbounded=True)
            members = [s for item in items if '_monthly_article_ids' not in item
                       for s in (item.get('sub_events') or [item])]
            scoped_ids = sorted({article_id for item in items for article_id in item.get('_monthly_article_ids', ())})
            vector = query_embedding(params['q']) if params.get('target_category') else None
            reports = event_articles(conn, members, params, vector, timeline=True,
                                     **({'scoped_ids': scoped_ids} if scoped_ids else {}))
            followup_items = [item for item in items if not item.get('_timeline_additional')]
            followups = event_followups(conn, followup_items + members, params)
    return reports, followups


def passes_result_filter(row: dict, min_cos: float, *, use_context_penalty: bool = False,
                         use_title_context: bool = False) -> bool:
    score = row['score']
    named = row.get('name_match') and not row.get('wrong_context')
    if use_title_context and named and row.get('context_match'):
        return True
    if score is None:
        return bool(row.get('person_context_match') or (named and row.get('context_match')))
    if use_context_penalty and row.get('wrong_context') and not row.get('context_match'):
        score = row['rank_score']
    return score >= max(-1., min_cos - (TITLE_MATCH_COS_MARGIN if named else 0))


def _search_db(conn, params: dict, vector: str | None, timeline: bool = False, *, count_only: bool = False,
               sort: str | None = None, cover_months: bool = False, stats_mode: str = 'full') -> dict:
    conditions, filter_values = article_filter(params)
    context_conditions, context_values = context_filter(params)
    conditions += context_conditions
    filter_values += context_values
    sports = national_team_sports(params['q'], params['keywords'])
    candidate_ids = None
    keyword_count = None
    timings = {}
    started = perf_counter()
    terms = core_terms(params) if params['mode'] != 'digest' else {}
    if not (timeline or count_only or stats_mode == 'only') and can_share_core_keyword_candidates(params, terms):
        sql, values = core_article_query(params, vector, terms, share_keyword_candidates=True)
        matches = conn.execute(sql, values).fetchall()
        core = {row['id']: row['title_core'] for row in matches if row['title_core'] is not None}
        candidate_ids = [row['id'] for row in matches]
    else:
        core = find_core_articles(conn, params, vector, terms)
    timings['core_ms'] = (perf_counter() - started) * 1000
    if stats_mode == 'only' and (core or params['mode'] == 'digest'):
        started = perf_counter()
        stats = collect_core_stats(conn, params, core, terms)
        timings['monthly_ms'] = (perf_counter() - started) * 1000
        return {'stats': stats, 'timings': timings}
    started = perf_counter()
    compound = compound_related_terms(conn, params, terms) if not core else {}
    lexical = related_article_ids(conn, params, compound or related_terms(params, terms)) if terms and not core else []
    timings['lexical_ms'] = (perf_counter() - started) * 1000
    started = perf_counter()
    if params["keywords"]:
        if candidate_ids is None:
            candidate_ids = keyword_candidate_ids(conn, params)
        keyword_count = len(candidate_ids)
        candidate_ids = list(dict.fromkeys((*candidate_ids, *core)))
    timings['keyword_candidates_ms'] = (perf_counter() - started) * 1000
    started = perf_counter()
    timings['semantic_candidates_ms'] = 0.0
    exact_alias_match = False
    ranking_params = params
    focus = topic_title_focus(params['q'])
    if params['mode'] == 'topic' and focus:
        subject, context, _, detail = focus
        focused_ids = [row['id'] for row in conn.execute(f'''
            SELECT a.id FROM articles a WHERE a.title ~* %s AND a.title ~* %s {conditions}
            ORDER BY (a.title ~* %s) DESC, a.published_date DESC NULLS LAST, a.id DESC LIMIT %s
        ''', (subject, context, *filter_values, detail, VECTOR_CANDIDATES)).fetchall()]
        if focused_ids:
            candidate_ids = focused_ids
            ranking_params = {**params, 'mode': 'combined'}
    if params['mode'] == 'topic' and (alias := params.get('web_alias')):
        pattern = title_entity_pattern((alias['term'], alias['canonical']))
        exact_ids = [row['id'] for row in conn.execute(f'''
            SELECT a.id FROM articles a WHERE a.title ~* %s {conditions}
            ORDER BY a.published_date DESC NULLS LAST, a.id DESC LIMIT %s
        ''', (pattern, *filter_values, VECTOR_CANDIDATES)).fetchall()]
        if exact_ids:
            exact_alias_match = True
            candidate_ids = exact_ids
            ranking_params = {**params, 'mode': 'combined'}
    if lexical or compound:
        candidate_ids = lexical
        if vector:
            ranking_params = {**params, 'mode': 'combined'}
    # 핵심 기사가 먼저 정렬되는 단일 후보 목록을 다 채우면 의미 후보를 추가해도 상위 기사는 같다.
    if core and params['mode'] == 'topic':
        if candidate_ids is None:
            if (len(core) >= VECTOR_CANDIDATES and not params.get('excluded_categories')
                    and not required_title_flag(params)[0]):
                candidate_ids = []
            else:
                semantic_started = perf_counter()
                sql, values = candidate_query(params, vector)
                candidate_ids = [row['id'] for row in conn.execute(sql, values).fetchall()]
                timings['semantic_candidates_ms'] += (perf_counter() - semantic_started) * 1000
        candidate_ids = list(dict.fromkeys((*candidate_ids, *core)))
        ranking_params = {**params, 'mode': 'combined'}
    sql, values = candidate_query(ranking_params, vector, candidate_ids, core=core, lexical=bool(lexical or compound),
                                  partial_terms=compound or None)
    ranking_started = perf_counter()
    rows = conn.execute(sql, values).fetchall()
    timings['ranking_ms'] = (perf_counter() - ranking_started) * 1000
    if (not compound and vector and params['mode'] in ('topic', 'combined')
            and (('required_terms' in params and core)
                 or (ranking_params['mode'] == 'combined' and not core and len(lexical) < 20))
            and not (sports or params.get('target_category') or params.get('excluded_categories'))):
        semantic_started = perf_counter()
        sql, values = candidate_query({**params, 'mode': 'topic'}, vector)
        existing = {row['id'] for row in rows}
        rows.extend(row for row in conn.execute(sql, values).fetchall() if row['id'] not in existing)
        timings['semantic_candidates_ms'] += (perf_counter() - semantic_started) * 1000
    for row in rows:
        row['tier'] = 'core' if row['id'] in core or params['mode'] == 'digest' else 'related'
        row['title_core'] = core.get(row['id'], False)
    if short_person_names(params):
        rows = [row for row in rows if row['tier'] == 'core' or row.get('partial_match')]
    ranked_count = rows[0].get("ranked_candidate_count", len(rows)) if rows and params["mode"] == "combined" else len(rows)
    timings["candidates_ms"] = (perf_counter() - started) * 1000
    min_cos = result_min_cos()
    if params.get('target_category') and params['mode'] == 'topic':
        for row in rows:
            if row.get('rank_score') is None:
                row['rank_score'] = row['score'] - CATEGORY_RANK_PENALTY * row['category_mismatch']
    if vector:
        for row in rows:
            rank = row.get('rank_score', row['score'])
            if rank is not None:
                row['rank_score'] = (rank + 0.035 * bool(row.get('additional_match'))
                                     - 0.07 * bool(row.get('short_title')) - 0.09 * bool(row.get('routine_title')))
    # 분류로 접힐 보도와 미분류 보도에는 기존 유사도 기준을 유지한다.
    contexts = context_words(params['q'])
    business_context = bool(params.get('excluded_categories') and contexts & BUSINESS_CONTEXT_WORDS)
    baseball_person = '야구선수' in contexts
    title_context = bool(focused_title_signals(params['q']) or topic_aspect_pattern(params['q']))
    title_priority = bool(focus or literal_title_focus(params))
    honorifics = surname_honorific_terms(params, terms)

    def relevant(row):
        if row['tier'] == 'core' or row.get('partial_match'):
            return True
        if honorifics:
            return False
        if terms and not (row.get('category_excluded') or row.get('person_context_match') is False):
            score = row['score']
            return (score is not None and score >= min_cos
                    and (not business_context or not row.get('wrong_context') or row.get('context_match')
                         or row['rank_score'] >= min_cos))
        return (not vector or exact_alias_match or (title_priority and row.get('aspect_match'))
                or (baseball_person and row.get('person_context_match'))
                or passes_result_filter(row, min_cos, use_title_context=title_context,
                                        use_context_penalty=business_context
                                        and not (row.get('category_excluded') or row.get('category_unclassified'))))

    result_rows = [row for row in rows if relevant(row)]
    similarity_count = len(result_rows)
    excluded_rows = [row for row in result_rows if row.get('category_excluded')]
    other_rows = [row for row in result_rows if not row.get('category_excluded')
                  and row.get('person_context_match') is False]
    result_rows = [row for row in result_rows if not row.get('category_excluded')
                   and row.get('person_context_match', True)]
    excluded_rows.sort(key=lambda row: (row.get('rank_score', row['score']), row['id']), reverse=True)
    events = fold_events(result_rows, params["mode"])
    if count_only:
        return {'event_count': min(len(events), params['limit'])}
    started = perf_counter()
    absence = (date_window_absence(conn, params, terms, has_related=bool(result_rows))
               if not core and stats_mode != 'only' else {})
    related_ids = None
    if not core and (lexical or result_rows) and params['mode'] != 'digest':
        # 전체 단어 일치 기사와 유사도 기준을 통과한 후보를 중복 없이 집계한다.
        related_ids = list(dict.fromkeys((*lexical, *(row['id'] for row in result_rows))))
    stats_plan = (params, core, terms, {'related_ids': related_ids} if related_ids is not None else {})
    monthly_candidates = []
    monthly_selection = (params['mode'] == 'keyword' and sort is None and not (params.get('date_from') or params.get('date_to'))
                         and bool(events) and all(event['score'] is None for event in events))
    monthly_started = perf_counter()
    stats = None
    monthly = []
    if stats_mode != 'deferred':
        stats_params, stats_core, stats_terms, stats_options = stats_plan
        stats = collect_core_stats(conn, stats_params, stats_core, stats_terms, **stats_options,
                                   **({'cover_months': True} if cover_months else {}))
        monthly_candidates = stats.pop('monthly_event_candidates', [])
        if monthly_selection:
            monthly = stats['monthly']
    elif monthly_selection and len(events) > params['limit']:
        monthly = monthly_counts_for_selection(conn, related_ids if related_ids is not None else core)
    timings['monthly_ms'] = (perf_counter() - monthly_started) * 1000
    timings['stats_ms'] = timings['core_ms'] + timings['lexical_ms'] + (perf_counter() - started) * 1000
    if stats_mode == 'only':
        return {'stats': stats, 'timings': timings}
    started = perf_counter()
    group_ids = [e["group_id"] for e in events if e["grouped"]]
    # 전체 article_groups를 집계하지 않고 반환할 사건만 group_id 인덱스로 일괄 조회.
    summaries = conn.execute(f"""
        SELECT wanted.group_id, summary.* FROM unnest(%s::bigint[]) AS wanted(group_id)
        CROSS JOIN LATERAL (
            SELECT count(*) AS article_count, max(g.summary) AS summary,
                   min(a.published_date) AS date_from, max(a.published_date) AS date_to,
                   COALESCE(max(g.article_id) FILTER (WHERE g.is_representative), min(a.id))
                       AS representative_id,
                   array_agg(DISTINCT s.publisher ORDER BY s.publisher)
                       FILTER (WHERE s.publisher IS NOT NULL) AS publishers
            FROM article_groups g JOIN articles a ON a.id=g.article_id
            LEFT JOIN article_sources s ON s.article_id=a.id
            WHERE g.group_id=wanted.group_id {conditions}
        ) summary
    """, (group_ids, *filter_values)).fetchall() if group_ids else []
    timings['group_lookup_ms'] = (perf_counter() - started) * 1000
    by_group = {s["group_id"]: s for s in summaries}
    for event in events:
        first = event["matches"][0]
        event.update((by_group.get(event["group_id"]) if event['grouped'] else None) or {
            "article_count": 1, "date_from": first["published_date"],
            "date_to": first["published_date"], "representative_id": first["id"],
        })
        event['publishers'] = event.get('publishers') or ([first['publisher']] if first.get('publisher') else [])
        event.update(event_coverage(event))
        if event['representative_id'] != first['id']:
            event.update(representative_id=first['id'], summary=None)
    events = sort_result_events(events, params["mode"], similarity=True, importance_tiebreak=sort != 'latest')
    # 핵심 사건은 보조어 근거와 제목 근거를 반영한다. 연관 사건도 같은 한도로 제공한다.
    primary = [event for event in events if event['tier'] == 'core']
    primary.sort(key=lambda event: (event['matches'][0].get('support_matches', 0),
                                    event['matches'][0].get('title_core', False)), reverse=True)
    related = [event for event in events if event['tier'] == 'related']
    limit = params['limit']
    if monthly_selection:
        primary = select_monthly_result_events(primary, monthly, limit)
        related = select_monthly_result_events(related, monthly, limit)
    elif timeline and (cover_months or sort != 'latest'):
        primary = select_timeline_events({'events': primary}, primary, limit=limit, preserve_order=True)
        related = select_timeline_events({'events': related}, related, limit=limit, preserve_order=True)
    else:
        primary, related = primary[:limit], related[:limit]
    events = primary + related
    timings["groups_ms"] = (perf_counter() - started) * 1000
    started = perf_counter()
    ids = sorted({e["representative_id"] for e in events} |
                 {m["id"] for e in events for m in e["matches"]} |
                 {row["id"] for row in excluded_rows + other_rows} |
                 {event["representative_id"] for event in monthly_candidates})
    details = conn.execute(f"""
        SELECT a.id, a.title, a.published_date, a.url, s.publisher, {article_evidence_column()},
               ARRAY(SELECT DISTINCT ({NORMALIZED_MAJOR_SQL}) || COALESCE(' > ' || NULLIF(btrim(c.minor), ''), '')
                     FROM article_categories c WHERE c.article_id=a.id ORDER BY 1) AS categories
        FROM articles a LEFT JOIN article_sources s ON s.article_id=a.id
        WHERE a.id=ANY(%s::bigint[])
    """, (ids,)).fetchall() if ids else []
    supporting = params.get('supporting_terms', [])
    for article in details:
        basis = 'date_scope' if params['mode'] == 'digest' else 'required_terms' if article['id'] in core else 'semantic'
        article['match_reason'] = article_match_reason(article, compound or terms, supporting, basis=basis)
    by_id = {article['id']: article for article in details}
    excluded_articles = [{**by_id[row['id']], 'score': row['score']} for row in excluded_rows]
    other_articles = [{**by_id[row['id']], 'score': row['score']} for row in other_rows]
    for event in events:
        event["representative"] = by_id[event["representative_id"]]
        event["matches"] = [{**m, **by_id[m["id"]]} for m in event["matches"]]
        if not event['publishers'] and (publisher := event['representative']['publisher']):
            event.update(publishers=[publisher])
            event.update(event_coverage(event))
    month_candidates = {}
    for event in monthly_candidates:
        article = by_id[event['representative_id']]
        month_candidates.setdefault(event['month'][:7], []).append({
            **{key: event[key] for key in ('group_id', 'grouped', 'date_from', 'date_to', 'article_count')},
            'tier': 'related' if stats['basis'] == 'related' else 'core',
            'representative': article, 'matches': [article], 'publishers': event['publishers'] or [],
            '_monthly_article_ids': event['article_ids'],
        })
    if not timeline:
        event_articles_started = perf_counter()
        reports = event_articles(conn, events, params, vector)
        timings['event_articles_ms'] = (perf_counter() - event_articles_started) * 1000
        for event in events:
            event['articles'] = reports.get((event['grouped'], event['group_id']), [])
    timings["details_ms"] = (perf_counter() - started) * 1000
    return {"events": primary, "related_events": related,
            **({'monthly_event_selection': True} if timeline and monthly_selection else {}),
            **({'timeline_month_candidates': month_candidates} if cover_months else {}),
            "candidate_count": len(rows), "excluded_candidate_count": len(rows) - similarity_count,
            "excluded_articles": excluded_articles, "excluded_categories": params.get('excluded_categories', []),
            **({'national_team_sports': sports, 'other_articles': other_articles} if sports else
               {'person_context': True, 'other_articles': other_articles} if person_title_flag(params)[0] else {}),
            "target_category": params.get('target_category'),
            "result_filter_relaxed": False,
            "result_min_cos": min_cos, "timings": timings,
            **({'stats': stats} if stats is not None else {'_stats_plan': stats_plan}),
            "keyword_candidate_count": keyword_count, "ranked_candidate_count": ranked_count,
            "candidate_limit": None, "candidate_limit_applied": False,
            "keyword_match": "article_keywords_exact" if params["keywords"] else None, **absence}


def _json_values(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_values(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def empty_search_result(basis: str) -> dict:
    return {"events": [], "related_events": [], "candidate_count": 0, "keyword_candidate_count": 0, "ranked_candidate_count": 0,
            "excluded_candidate_count": 0, "result_filter_relaxed": False, "result_min_cos": result_min_cos(),
            "candidate_limit": None, "candidate_limit_applied": False, "keyword_match": None,
            "timings": {}, "stats": {"basis": basis, "monthly": []}}


def person_result_branches(conn, params: dict) -> list[dict]:
    """버튼 건수에 맥락을 넣어 검색했을 때와 같은 판정·표시 상한을 적용한다."""
    name, = params['keywords']
    branches = []
    for context in PERSON_BRANCH_CONTEXTS:
        topic = f'{context} {name}'
        scoped = {**params, 'q': topic, 'mode': 'combined', 'excluded_categories': excluded_categories(topic)}
        count = _search_db(conn, scoped, query_embedding(topic), count_only=True)['event_count']
        if count >= PERSON_BRANCH_MIN_EVENTS:
            branches.append({'topic': topic, 'count': count})
    return branches if len(branches) >= 2 else []


def search_snapshot_results(conn, params, vector, *, timeline, sort, cover_months, include_followups, stats_mode):
    if coverage := missing_article_coverage(conn, params):
        result = {**empty_search_result('지정 발행일·언론사 조건'), 'data_absence': coverage,
                  **article_coverage_notice(conn, params)}
        if stats_mode != 'only' and params['mode'] != 'digest':
            absence = date_window_absence(conn, params, core_terms(params))
            if absence.get('nearest_events'):
                result.update(absence)
        if aspects := stats_aspects(params):
            result['stats']['aspects'] = list(aspects)
        return result
    result = _search_db(conn, params, vector, timeline=timeline, sort=sort,
                        **({'cover_months': True} if cover_months else {}),
                        **({'stats_mode': stats_mode} if stats_mode != 'full' else {}))
    if stats_mode == 'only':
        return result
    events = result['events'] + result.get('related_events', [])
    if include_followups and events:
        started = perf_counter()
        try:
            with conn.transaction():
                followups = event_followups(conn, events, params)
        except psycopg.Error:
            result['followups_error'] = '후속 기사를 불러오지 못했습니다.'
        else:
            for event in events:
                event['followups'] = followups.get(followup_key(event), [])
        result['timings']['followups_ms'] = (perf_counter() - started) * 1000
    names = params['keywords']
    if not timeline and not params['q'] and len(names) == 1 and names[0] in REGISTERED_PERSON_NAMES:
        try:
            with conn.transaction():
                branches = person_result_branches(conn, params)
        except Exception:
            result['_branch_failed'] = True
        else:
            if branches:
                result['person_branches'] = branches
    return result


def search(params: dict, *, timeline: bool = False, web_alias: dict | None = None, term_partition: dict | None = None,
           include_followups: bool = False, sort: str | None = None, cover_months: bool = False,
           stats_mode: str = 'full') -> dict:
    start = perf_counter()
    metadata = {}
    conversion_ms = 0.0
    if isinstance(params, dict) and "ask" in params:
        params = dict(params)
        ask = params.pop("ask")
        if not isinstance(ask, str) or len(ask) > 500:
            raise SearchError("질문은 500자 이내로 입력하세요.")
        # 수동 입력도 먼저 검증해 AI 장애 시 안전하게 그대로 사용할 수 있게 한다.
        validate_params(params)
        if ask.strip():
            conversion_start = perf_counter()
            try:
                conversion = convert_question(ask)
                params.update(keywords=conversion["keywords"], q=conversion["q"],
                              **{k: params.get(k) or conversion.get(k) for k in ('date_from', 'date_to')})
                params.pop("mode", None)
                metadata["conversion"] = conversion
            except LLMError as exc:
                if validate_params(params)['mode'] == 'empty':
                    raise QuestionConversionError(
                        '질문을 해석하지 못했습니다. 질문 해석 모델(Ollama) 상태를 확인한 후 다시 시도하세요.'
                    ) from None
                metadata["ai_error"] = str(exc)
            conversion_ms = (perf_counter() - conversion_start) * 1000
    params = validate_params(params)
    if params["mode"] == "empty":
        result = empty_search_result('검색 조건 없음')
        if stats_mode == 'deferred':
            result.pop('stats')
        return {"params": params, **result, "elapsed_ms": 0, "read_only": True, **metadata}
    try:
        context_start = perf_counter()
        converted = metadata.get('conversion', {})
        alias = converted.get('web_alias') or web_alias
        search_params = {**params, 'web_alias': alias} if alias else params
        if 'required_terms' in converted:
            term_partition = {key: converted[key] for key in ('required_terms', 'supporting_terms', 'person_names',
                                                             'term_source', 'target_category') if key in converted}
        if term_partition is not None:
            source = params['q'] or ' '.join(params['keywords'])
            people = term_partition.get('person_names', ())
            if 'supporting_terms' in term_partition:
                supporting = dict.fromkeys(' '.join(QUERY_ACTION_FILLER.sub(' ', word).split())
                                           for word in term_partition['supporting_terms'])
                term_partition = {**term_partition,
                                  'supporting_terms': list(dict.fromkeys(grounded for word in supporting
                                                       if (grounded := ground_query_term(word, source, people))))}
            if 'required_terms' in term_partition:
                term_partition = {**term_partition, 'required_terms': normalize_required_terms(
                    term_partition['required_terms'], term_partition.get('supporting_terms', []), source, people)}
            search_params = {**search_params, **term_partition}
            metadata['term_partition'] = term_partition
        if params['q'] and params['mode'] != 'digest':
            sports = national_team_sports(params['q'], params['keywords'])
            excluded = [] if sports else excluded_categories(params['q'])
            if excluded:
                search_params = {**search_params, 'excluded_categories': excluded}
            elif not sports and not context_words(params['q']):
                target = search_params.get('target_category')
                if not target and 'conversion' not in metadata and term_partition is None:
                    context = infer_person_context(params['q'], cache_only=True) if timeline else infer_person_context(params['q'])
                    target = context[1] if context else None
                    if context:
                        search_params = {**search_params, 'person_names': [context[0]]}
                if target:
                    search_params = {**search_params, 'target_category': target}
        context_ms = (perf_counter() - context_start) * 1000
        embedding_ms = duplicates_ms = 0.0
        connection_start = perf_counter()
        env_file = Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV)))
        with psycopg.connect(
            **connection_options(env_file), autocommit=True, row_factory=dict_row,
            connect_timeout=10, application_name="dice_readonly_mock_search",
            options="-c default_transaction_read_only=on -c statement_timeout=10000",
        ) as conn:
            conn.read_only = True
            connection_ms = (perf_counter() - connection_start) * 1000
            snapshot_start = perf_counter()
            prior = article_data_snapshot(conn)
            snapshot_ms = (perf_counter() - snapshot_start) * 1000
            transaction_start = perf_counter()
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                # 월×키워드 정렬 유출과 JIT 준비 비용을 줄이고 임베딩 조인의 공유 메모리 한도를 피한다.
                conn.execute("SET LOCAL jit = off; SET LOCAL work_mem = '32MB'; SET LOCAL enable_parallel_hash = off")
                if params["mode"] == "topic":
                    conn.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
                if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
                    raise SearchError("읽기 전용 트랜잭션을 확인하지 못했습니다.")
                connection_ms += (perf_counter() - transaction_start) * 1000
                snapshot_start = perf_counter()
                duplicate_key, article_key = stable_article_data_snapshot(conn, prior)
                snapshot_ms += (perf_counter() - snapshot_start) * 1000
                cache_key = (QUERY_REVISION, article_key,
                             json.dumps(search_params, sort_keys=True, ensure_ascii=False),
                             timeline, sort, cover_months, include_followups)
                use_cache = article_key is not None and not timeline and 'ai_error' not in metadata
                cached = cached_search_result((*cache_key, stats_mode)) if use_cache else None
                if os.environ.get('SEARCH_SNAPSHOT_DIAGNOSTICS') == '1':
                    logger.warning('search cache diagnostic pid=%s duplicate_key=%s result_key=%s hit=%s',
                                   os.getpid(), duplicate_key is not None, article_key is not None,
                                   cached is not None)
                if cached is not None:
                    result, _ = cached
                    result['timings'] = {}
                else:
                    deferred = (cached_search_result((*cache_key, 'deferred'))
                                if use_cache and stats_mode == 'only' else None)
                    if deferred is not None and deferred[1] is not None:
                        stats_params, core, terms, options = deferred[1]
                        monthly_start = perf_counter()
                        stats = collect_core_stats(conn, stats_params, core, terms, **options)
                        result = {'stats': stats, 'timings': {'monthly_ms': (perf_counter() - monthly_start) * 1000}}
                    else:
                        embedding_start = perf_counter()
                        vector = query_embedding(params["q"]) if params["q"] and params['mode'] != 'digest' else None
                        embedding_ms = (perf_counter() - embedding_start) * 1000
                        duplicates_start = perf_counter()
                        scoped = with_article_duplicates(conn, search_params, unbounded=include_followups,
                                                         reuse_snapshot=True, snapshot_key=duplicate_key)
                        duplicates_ms = (perf_counter() - duplicates_start) * 1000
                        result = search_snapshot_results(
                            conn, scoped, vector, timeline=timeline, sort=sort, cover_months=cover_months,
                            include_followups=include_followups, stats_mode=stats_mode)
                    stats_plan = result.pop('_stats_plan', None)
                    branch_failed = result.pop('_branch_failed', False)
                    if use_cache and not branch_failed and 'followups_error' not in result:
                        cache_search_result((*cache_key, stats_mode), result, stats_plan)
        result["timings"].update(embedding_ms=embedding_ms, connection_ms=connection_ms,
                                 duplicates_ms=duplicates_ms, conversion_ms=conversion_ms, context_ms=context_ms,
                                 snapshot_ms=snapshot_ms)
        result['cache_hit'] = cached is not None
    except SearchError:
        raise
    except Exception:
        logger.exception("검색 처리 중 예외가 발생했습니다.")
        raise SearchError("검색 실패: 로컬 모델 캐시·CUDA·DB 연결·읽기 권한을 확인하세요.") from None
    if stats_mode == 'deferred':
        result.pop('stats', None)
    return _json_values({"params": params, **result, "elapsed_ms": round((perf_counter()-start)*1000, 1),
                         "read_only": True, **metadata})


def self_test() -> None:
    """DB/모델 없이 입력·순서·접기·정확/HNSW 경로를 점검한다."""
    from unittest.mock import MagicMock, patch
    from io import BytesIO

    captured = []

    def fake_http(request, timeout):
        captured.append((json.loads(request.data), timeout))
        return BytesIO(json.dumps({'message': {'content': '{"q":"주제","keywords":[]}',
                                               'thinking': '검색 조건 검토'}}).encode())

    with patch.dict(globals(), urlopen=fake_http), patch.dict(os.environ, LLM_THINK=''):
        assert ollama_chat('지시', '질문', ASK_SCHEMA)[0]['q'] == '주제'
        with patch.dict(os.environ, LLM_THINK='1'):
            assert ollama_chat('지시', '질문', ASK_SCHEMA)[0]['q'] == '주제'
    assert captured[0][0]['think'] is False and captured[0][0]['stream'] is False
    assert captured[1][0]['think'] is True
    assert captured[0][0]['options']['temperature'] == 0 and captured[0][1] <= LLM_TIMEOUT
    assert captured[0][0]['messages'] == [
        {'role': 'system', 'content': '지시'}, {'role': 'user', 'content': '"질문"'},
    ]
    for failure in (TimeoutError(), URLError('연결 거절'), ValueError('내부 응답')):
        with patch.dict(globals(), urlopen=lambda *_a, **_kw: (_ for _ in ()).throw(failure)):
            try:
                ollama_chat('지시', '질문', ASK_SCHEMA)
            except LLMError as exc:
                assert '내부 응답' not in str(exc) and not _llm_lock.locked()
            else:
                raise AssertionError('LLM 오류를 허용함')

    converted = postprocess_question({"keywords": ["요즘 러브버그", "미확인", "대통령"], "q": "대량 발생"},
                                    lambda words: {"러브버그": 128, "대통령": 50001})
    assert converted['keywords'] == ['러브버그'] and '대통령' in converted['q']
    assert {r['reason'] for r in converted['removed_keywords']} == {'일반어', 'DB 키워드 없음', '일치 기사 5만 건 초과'}
    assert postprocess_question({'keywords': ['뉴스', '내용'], 'q': '수면 우울증'}, lambda _: {})['keywords'] == []
    for raw in ({}, {'keywords': '문자열', 'q': '주제'}, {'keywords': [], 'q': ' '}):
        try:
            postprocess_question(raw, lambda _: {})
        except LLMError:
            pass
        else:
            raise AssertionError('잘못된 변환 응답 허용')
    event = dict(group_id=1, date_from='2026-01-01', date_to='2026-01-02', article_count=9,
                 publishers=['동아일보', '다른신문'], representative=dict(title='강선우 1심 판결', publisher='동아일보', url='https://example.com'),
                 matches=[dict(title='강선우 2년 구형')])
    def merge_case(gid, start, count=1, end=None):
        return dict(event, group_id=gid, grouped=True, date_from=start, date_to=end or start,
                    article_count=count, representative=dict(event['representative'], title=f'사건 {gid}'))

    a = merge_case(1, '2026-01-01')
    b = merge_case(2, '2026-01-04')
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = [
        dict(grouped=True, group_id=1, center='[3,4]'),
        dict(grouped=False, group_id=2, center='[0,2]')]
    with patch.object(psycopg, 'connect', return_value=connection), patch.dict(globals(), connection_options=lambda _p: {}, article_duplicate_ids=lambda *_args: []):
        loaded = timeline_centers([a, dict(b, grouped=False)])
    assert loaded == {(True, 1): [.6, .8], (False, 2): [0., 1.]}
    assert connection.read_only is True and connection.execute.call_count == 3
    sql, values = connection.execute.call_args.args
    assert 'avg(e.embedding)' in sql and 'GROUP BY' in sql and 'unnest' in sql
    assert values == ([1], [2])
    connection.reset_mock()
    connection.execute.return_value.fetchall.return_value = [
        dict(grouped=True, group_id=1, keyword='김경'),
        dict(grouped=False, group_id=2, keyword='김경')]
    with patch.object(psycopg, 'connect', return_value=connection) as connect, patch.dict(
            globals(), connection_options=lambda _p: {}, article_duplicate_ids=lambda *_args: []):
        features = timeline_keywords([a, a, dict(b, grouped=False)], ['강선우', '공천헌금'])
    assert features == {(True, 1): {'김경'}, (False, 2): {'김경'}}
    assert connection.execute.call_count == 3 and connection.read_only is True
    assert connect.call_args.kwargs['options'] == '-c default_transaction_read_only=on'
    sql, values = connection.execute.call_args.args
    assert values[:3] == ([1], [2], 10)
    assert set(values[3]) == KEYWORD_STOPWORDS | MERGE_GENERAL_WORDS | {'강선우', '공천헌금'}
    assert 'sum(k.mention_count)' in sql and 'UNION ALL' in sql and 'unnest' in sql
    assert 'ORDER BY mentions DESC, keyword COLLATE "C"' in sql
    # 먼저 전체 상위 10개를 매긴 후 제외한다. 제외어를 제거한 뒤 순위를 다시 매기지 않는다.
    assert sql.index('row_number()') < sql.index('NOT (keyword=ANY')
    with patch.object(psycopg, 'connect') as connect:
        assert timeline_keywords([], []) == {} and not connect.called
    features = {(True, i): {'공유'} for i in range(1, 5)}
    boundary = [MERGE_MIN_COS, math.sqrt(1 - MERGE_MIN_COS ** 2)]
    centers = {(True, 1): [1., 0.], (True, 2): boundary}
    assert len(merge_timeline_events([b, a], centers, features)) == 1  # 3일·0.88·공유 1개 포함
    assert len(merge_timeline_events([a, b], centers, {})) == 2  # 빈 특징어는 병합하지 않음
    assert len(merge_timeline_events([a, b], centers, {(True, 1): {'김병기'}, (True, 2): {'김경'}})) == 2
    assert len(merge_timeline_events([a, dict(b, date_from='2026-01-05', date_to='2026-01-05')], centers, features)) == 2
    assert len(merge_timeline_events([a, b], {**centers, (True, 2): normalized_vector([.879999, math.sqrt(1-.879999**2)])}, features)) == 2
    assert len(merge_timeline_events([a, b], {}, features)) == 2
    # 긴 사건의 마지막 날짜 기준, 겹치는 기간, 동일 ID 중복도 점검한다.
    long_a = dict(a, date_to='2026-01-10')
    assert len(merge_timeline_events([long_a, merge_case(2, '2026-01-13')], centers, features)) == 1
    assert len(merge_timeline_events([long_a, b, a], centers, features)) == 1
    # 누적 기간은 끝-시작 일수로 계산한다. 14일 허용, 15일 거부(겹치는 기간도 포함).
    span_start = dict(a, date_to='2026-01-13')
    at_limit = merge_case(2, '2026-01-15')
    within = merge_timeline_events([span_start, at_limit], centers, features)
    assert len(within) == 1 and within[0]['merge_checks'][0]['span_days'] == 14
    assert len(merge_timeline_events([span_start, merge_case(2, '2026-01-16')], centers, features)) == 2
    overlapping = merge_case(2, '2026-01-04', end='2026-01-16')
    assert len(merge_timeline_events([span_start, overlapping], centers, features)) == 2
    # 긴 원 묶음은 쪼개지 않지만, 그 안에 포함되는 다음 사건도 추가 병합하지 않는다.
    assert len(merge_timeline_events([dict(a, date_to='2026-01-20'), b], centers, features)) == 2
    daily = [merge_case(i, f'2026-01-{i:02}') for i in range(1, 30)]
    split = merge_timeline_events(daily, {(True, i): [1.] for i in range(1, 30)},
                                 {(True, i): {'공유'} for i in range(1, 30)})
    assert [i['event_count'] for i in split] == [15, 14]
    assert [(i['date_from'], i['date_to']) for i in split] == [
        ('2026-01-01', '2026-01-15'), ('2026-01-16', '2026-01-29')]
    chain = [merge_case(1, '2026-01-01', 2), merge_case(2, '2026-01-04', 8),
             merge_case(3, '2026-01-07', 4), merge_case(4, '2026-01-10', 1)]
    v = normalized_vector([.9, math.sqrt(1 - .9 ** 2)])
    chain_centers = {(True, 1): [1., 0.], (True, 2): v, (True, 3): [1., 0.], (True, 4): v}
    joined = merge_timeline_events(chain, chain_centers, features)[0]
    assert joined['event_count'] == 4 and joined['article_count'] == 15
    assert joined['representative']['title'] == '사건 2'
    expected = normalized_vector([2 + 8 * v[0], 8 * v[1]])
    assert math.isclose(joined['merge_checks'][1]['cosine'], expected[0], abs_tol=1e-12)
    expected = normalized_vector([6 + 8 * v[0], 8 * v[1]])
    assert math.isclose(joined['merge_checks'][2]['cosine'], sum(x*y for x, y in zip(expected, v)), abs_tol=1e-12)
    assert len(joined['sub_events']) == 4 and joined['date_to'] == '2026-01-10'
    # 첫 사건의 특징어도 끝까지 보존하며, 다음 사건의 특징어도 합집합에 추가한다.
    union_features = {(True, 1): {'초기'}, (True, 2): {'초기', '중간'},
                      (True, 3): {'중간', '후기'}, (True, 4): {'초기'}}
    joined = merge_timeline_events(chain, chain_centers, union_features)[0]
    assert joined['event_count'] == 4
    assert [c['shared_keywords'] for c in joined['merge_checks']] == [['초기'], ['중간'], ['초기']]
    # 주제가 있으면 최고 유사도부터, 없으면 보도량부터 비교한다.
    many = [merge_case(i, f'2026-01-{i:02}') for i in range(1, 17)]
    assert len(timeline_candidates(dict(events=list(reversed(many))))) == 15
    assert timeline_candidates(dict(events=list(reversed(many))))[0]['group_id'] == 2
    assert TIMELINE_LIMIT == 10
    ranked = list(reversed(many[:12]))
    selected = selected_timeline_events(dict(events=ranked))
    assert [e['group_id'] for e in selected] == list(range(3, 13))
    merged_ranked = merge_timeline_events(ranked, {}, {})
    merged_ranked[0]['sub_events'].append(merged_ranked.pop()['sub_events'][0])
    selected = select_timeline_events(dict(events=ranked), merged_ranked)
    assert [e['group_id'] for e in selected] == [1, *range(3, 12)]
    assert selected[0]['sub_events'][-1]['group_id'] == 12
    volume = [dict(e, article_count=1, score=.1) for e in ranked]
    volume[-1] = dict(volume[-1], article_count=74, score=.2)
    selected = selected_timeline_events(dict(events=volume, params={'q': ''}))
    assert 1 in [e['group_id'] for e in selected] and 3 not in [e['group_id'] for e in selected]
    conflicting = [dict(e, article_count=100 if e['group_id'] == 11 else 1,
                        score=.99 if e['group_id'] == 1 else .9) for e in ranked]
    assert [e['group_id'] for e in select_timeline_events(
        dict(events=conflicting, params={'q': '주제'}), conflicting, limit=1)] == [11]
    assert [e['group_id'] for e in select_timeline_events(
        dict(events=conflicting, params={'q': '주제'}), conflicting, limit=2)] == [1, 11]
    tied = [dict(e, article_count=1, score=.1) for e in ranked]
    tied[-1] = dict(tied[-1], score=.9)
    assert 1 in [e['group_id'] for e in selected_timeline_events(dict(events=tied, params={'q': ''}))]
    assert 1 in [e['group_id'] for e in selected_timeline_events(dict(events=tied, params={'q': '주제'}))]
    tied[-2] = dict(tied[-2], score=.9)
    assert 2 in [e['group_id'] for e in selected_timeline_events(dict(events=tied, params={'q': '주제'}))]
    equally_scored = [dict(e, score=.9) for e in tied]
    assert 1 not in [e['group_id'] for e in selected_timeline_events(dict(events=equally_scored, params={'q': '주제'}))]
    merged_tied = merge_timeline_events(tied, {}, {})
    merged_tied[0]['sub_events'].append(merged_tied.pop()['sub_events'][0])
    chosen = select_timeline_events(dict(events=tied, params={'q': '주제'}), merged_tied)
    assert chosen[0]['group_id'] == 1 and chosen[0]['sub_events'][-1]['group_id'] == 12
    result = dict(events=[event], params=dict(keywords=['강선우'], q='', limit=20))
    raw = {'months': [], 'summary': '짧음'}
    timeline = match_timeline(result)
    assert len(timeline['items']) == 1 and timeline['items'][0]['date_label'] == '2026-01-01 ~ 2026-01-02'
    assert timeline['items'][0]['source_label'] == '동아일보 외 1개 언론사'
    assert timeline['items'][0]['title'] == event['representative']['title']
    assert 'label' not in timeline['items'][0] and 'summary' not in timeline['items'][0]
    month = dict(month='2026-01', article_count=9, event_count=2, events=[
        dict(title='[단독] “강선우” 공천헌금 의혹 경찰 수사 착수', other_titles=[
            '강선우 공천헌금 1,000만원 수수 의혹', '강선우 “혐의 부인” 입장 밝혀'], article_count=7),
        dict(title='강선우 귀국, 경찰 조사 예정', other_titles=[], article_count=2)])
    good_month = '공천 과정에서 금품을 받았다는 의혹이 불거졌다'
    assert validate_month_text(good_month, month) == []  # 제목에 없는 단어도 허용
    assert any('숫자' in r for r in validate_month_text('강선우 금품 수수액은 2000만원으로 알려졌다', month))
    assert '다른 달 언급' in validate_month_text('2월에는 공천 과정에서 금품 수수 의혹이 불거졌다', month)
    assert '그 달 근거에 없는 인용구' in validate_month_text('강선우는 “혐의 인정”이라는 입장을 밝혔다', month)
    assert '그 달 근거에 없는 인용구' not in validate_month_text('강선우는 “혐의 부인” 입장을 드러냈다', month)
    assert '그 달 근거에 없는 인용구' in validate_month_text('강선우는 “혐의 부인 입장을 드러냈다', month)
    assert not any('숫자' in r for r in validate_month_text('강선우가 받은 금액은 1000 만원으로 전해졌다', month))
    assert any('숫자' in r for r in validate_month_text('강선우가 받은 금액은 1000 억원으로 전해졌다', month))
    assert month_number_tokens('１,０００ 만원 3 월') == {'1000만원', '3월'}
    assert validate_month_text('가' * 32, month) == []
    assert validate_month_text('가' * 33, month) == ['text 문자열 형식 위반']
    assert validate_month_text(None, month) == ['text 문자열 형식 위반']
    fallback = clean_month_title(month['events'][0]['title'])
    assert fallback == '강선우 공천헌금 의혹 경찰 수사 착수' and '…' not in fallback
    assert clean_month_title('[속보] “삼성전자” 휴대폰에서 번 돈…') == '삼성전자 휴대폰에서 번 돈'
    long_title = '삼성전자 휴대폰 ' + '신제품 출시 관련 보도 ' * 5
    cut = clean_month_title(long_title)
    assert len(cut) <= 45 and long_title.startswith(cut) and long_title[len(cut)].isspace()
    assert '삼성전자' in cut and '휴대폰' in cut and '…' not in cut
    month['events'] = [dict(item, date_from=f'2026-01-{day:02}', date_to=f'2026-01-{day:02}')
                       for day, item in zip((5, 20), month['events'])]
    assert match_timeline_months({}, [month])['months'][0]['text'] == (
        f'1/5 {fallback} → 1/20 강선우 귀국, 경찰 조사 예정')
    assert match_timeline_months({'months': None}, [month])['month_fallback_count'] == 1
    second = dict(event, group_id=2, date_from='2025-12-01', date_to='2025-12-01')
    assert timeline_summary_input(selected_timeline_events(dict(result, events=[event, second])))[0]['date_from'] == '2025-12-01'
    with patch.dict(globals(), _timeline_cache=OrderedDict(), timeline_centers=lambda _events: {},
                    timeline_keywords=lambda _events, _required: {}, keyword_timeline_candidates=lambda _p: [event]):
        with patch.dict(globals(), ollama_chat=lambda *_a, **_kw: (raw, 1.2)):
            assert not generate_timeline(result)['cached']
        with patch.dict(globals(), ollama_chat=lambda *_a: (_ for _ in ()).throw(AssertionError('캐시 미사용'))):
            assert generate_timeline(result)['cached']
    with patch.dict(globals(), _timeline_cache=OrderedDict(),
                    TIMELINE_RELEVANCE_BATCH=12,
                    timeline_centers=lambda es: {(e.get('grouped', True), e['group_id']): [1.0] for e in es},
                    query_embedding=lambda _q: '[1]',
                    timeline_keywords=lambda _events, _required: {}, ollama_chat=lambda *_a: (raw, 1.2)):
        q_only = dict(events=[dict(e, score=.9) for e in ranked], params=dict(keywords=[], q='주제', limit=20))
        q_timeline = generate_timeline(q_only)
        assert [e['group_id'] for e in q_timeline['items']] == list(range(3, 13))
        assert q_timeline['relevance_status'] == '판정 실패' and q_timeline['llm_calls'] == 1
        with patch(__name__ + '.ollama_chat', side_effect=LLMError('응답 시간 초과')) as chat:
            q_only['events'] = conflicting
            fallback = generate_timeline(q_only)
            assert [e['group_id'] for e in fallback['items']] == [1, *range(4, 13)]
            assert fallback['relevance_status'] == '판정 실패' and fallback['llm_ms'] >= 0
            assert chat.call_count == 1 and not fallback['cached']
        decisions = {str(i): 0 for i in range(1, 13)}
        with patch(__name__ + '.ollama_chat', return_value=(decisions, 2)) as chat:
            empty = generate_timeline(q_only)
            assert empty['items'] == [] and empty['no_relevant_events']
            assert empty['relevance_excluded_count'] == 12 and empty['llm_calls'] == 1 and chat.call_count == 1
            assert generate_timeline(q_only)['cached'] and chat.call_count == 1
        q_only['params']['q'] = '다른 주제'
        decisions = {**decisions, '1': 2, '999': 2, '-3': 1}
        with patch(__name__ + '.ollama_chat', side_effect=[(decisions, 2), (raw, 1)]) as chat:
            judged = generate_timeline(q_only)
            assert [e['group_id'] for e in judged['items']] == [1] and judged['relevance_excluded_count'] == 11
            assert judged['llm_calls'] == 2 and judged['relevance_ms'] == 2 and judged['llm_ms'] >= 3
            summary_payload = chat.call_args.args[1]
            assert 'flow' not in summary_payload and len(summary_payload['events']) == 1
    # 흐름 요약의 근거 검사: 날짜 월만 허용 예외, 숫자 단위·인용·월·키워드는 각각 독립 검사.
    summary_inputs = [dict(date_from='2026-01-30', date_to='2026-03-01',
                           title='강선우 공천헌금 1억 수사 시작', other_titles=['강선우 “혐의 부인” 2년 6개월 구형'])]
    good = '1월 강선우 공천헌금 1억 수사 시작이 보도됐다. 이어 강선우의 “혐의 부인”과 2년 6개월 구형이 보도됐다.'
    assert validate_timeline_summary(good, summary_inputs, ['강선우', '공천헌금']) == []
    assert validate_timeline_summary(good + ' 2026년 1월 30일과 3월 1일, 2026 30 3 1.', summary_inputs, []) == []
    assert any('31일' in reason for reason in validate_timeline_summary(good + ' 31일.', summary_inputs, []))
    assert '항목 날짜에 없는 월일' in validate_timeline_summary(good + ' 3월 30일.', summary_inputs, [])
    assert any('999' in reason for reason in validate_timeline_summary(good + ' 999.', summary_inputs, []))
    assert validate_timeline_summary(good.replace('1억', '9억'), summary_inputs, []) == ['입력 근거에 없는 숫자·단위: 9억']
    assert validate_timeline_summary(good.replace('2년', '2억'), summary_inputs, []) == ['입력 근거에 없는 숫자·단위: 2억']
    assert validate_timeline_summary(good.replace('혐의 부인', '혐의 인정'), summary_inputs, []) == ['입력 근거에 없는 인용구']
    assert validate_timeline_summary(good.replace('”', ''), summary_inputs, []) == ['입력 근거에 없는 인용구']
    assert validate_timeline_summary(good.replace('1월', '10월'), summary_inputs, []) == ['항목 날짜 범위에 없는 월']
    assert validate_timeline_summary(good.replace('1월', '2월'), summary_inputs, []) == []  # 기간 중간 월
    assert validate_timeline_summary(good, summary_inputs, ['트럼프']) == ['필수 키워드 최소 1개 누락']
    assert validate_timeline_summary('짧음', summary_inputs, []) == ['길이 40~250자 위반']
    assert validate_timeline_summary('가' * 250, summary_inputs, []) == []
    assert validate_timeline_summary('가' * 251, summary_inputs, []) == ['길이 40~250자 위반']
    assert validate_timeline_summary(None, summary_inputs, []) == ['summary 문자열 형식 위반']
    assert summary_number_tokens('1월 2년 6개월 25% 3회 1심 1,000만원') == {'2년', '6개월', '25%', '3회', '1심', '1000만원'}
    assert summary_number_tokens('100여대 24일 100마리 3번 1.5%p') == {'100여대', '24일', '100마리', '3번', '1.5%p'}
    order_inputs = [dict(date_from=day, date_to=day, title=title, other_titles=[])
                    for day, title in [
                        ('2026-02-12', '강선우 공천헌금 구속영장 신청'),
                        ('2026-03-03', '강선우 공천헌금 구속'),
                        ('2026-09-13', '강선우 징역 4년 구형'),
                        ('2026-09-20', '강선우 6개월 만에 보석 석방'),
                        ('2026-10-14', '강선우 1심 징역 2년 6개월 선고')]]
    actual_error = '1심에서 징역 2년 6개월을 선고받은 후 6개월 만에 보석 석방'
    assert validate_summary_stage_order(actual_error, order_inputs) == ['사건 단계 순서 역전']
    wrong_order = f'강선우는 {actual_error}됐다는 소식이 보도됐다.'
    assert validate_timeline_summary(wrong_order, order_inputs, ['강선우']) == ['사건 단계 순서 역전']
    right_order = '강선우는 구속영장 청구 이후 구속됐으며 징역 4년 구형이 보도됐다. 보석으로 석방된 뒤 1심에서 징역 2년 6개월을 선고받았다.'
    assert validate_timeline_summary(right_order, list(reversed(order_inputs)), ['강선우']) == []
    assert timeline_stages('구속 영장 신청 후 구속영장 청구, 구속 기소, 불구속 기소') == ['영장청구', '영장청구', '기소', '기소']
    assert timeline_stages('2심 판결 후 항소심 선고, 3심 판결 후 상고심 선고') == ['항소', '항소', '상고', '상고']
    assert validate_summary_stage_order('구속영장 청구 후 구속', order_inputs) == []
    assert validate_summary_stage_order('구속영장 신청 후 구속', [
        dict(item, title=item['title'].replace('신청', '청구')) for item in order_inputs]) == []
    assert validate_timeline_summary(right_order.replace('보석으로', '항소 후 보석으로'), order_inputs,
                                     ['강선우']) == ['타임라인에 없는 사건 단계: 항소']
    assert validate_summary_stage_order('구속 후 구속영장 신청', order_inputs) == ['사건 단계 순서 역전']
    assert validate_summary_stage_order('선고 후 보석', order_inputs) == ['사건 단계 순서 역전']
    assert validate_summary_stage_order('보석 뒤 구형', order_inputs) == ['사건 단계 순서 역전']
    assert validate_summary_stage_order('구형 다음 보석, 석방 이후 선고', order_inputs) == []
    assert validate_summary_stage_order('수사 뒤 조사', [dict(order_inputs[0], label='수사', other_titles=['조사'])]) == []
    assert validate_summary_stage_order('조사 후 수사', [dict(order_inputs[0], title='수사'),
        dict(order_inputs[1], title='조사 수사')]) == ['사건 단계 순서 역전']  # 첫 등장 기준
    all_children = dict(order_inputs[0], title='진행 상황', other_titles=[],
                        sub_events=[dict(representative={'title': t}) for t in ['경과', '현황', '압수수색']])
    assert validate_summary_stage_order('수사', [all_children]) == []  # 세 번째 하위 제목도 포함
    assert validate_summary_stage_order('인하 후 인상', [dict(order_inputs[0], title='관세 인상 발표'),
        dict(order_inputs[1], title='관세 인하 합의')]) == ['사건 단계 순서 역전']
    failed_order = match_timeline_summary({'summary': wrong_order}, order_inputs, ['강선우'])
    assert failed_order['lead'] is None and failed_order['summary_attempts'] == 1
    assert failed_order['summary_checks'][0]['reasons'] == ['사건 단계 순서 역전']
    child_events = [dict(event, group_id=i, representative={'title': f'하위 {i}'}) for i in range(8)]
    summary_source = timeline_summary_input([dict(event, sub_events=child_events)])
    assert summary_source[0]['other_titles'] == [f'하위 {i}' for i in range(6)]
    monthly_source = timeline_month_input([dict(event, sub_events=child_events)])
    assert monthly_source[0]['events'][0]['other_titles'] == [f'하위 {i}' for i in range(6)]
    assert monthly_source[0]['article_count'] == event['article_count']
    summary = match_timeline_summary({'summary': good}, summary_inputs, ['강선우'])
    assert summary['summary_status'] == '통과' and summary['lead'] == good
    for bad_summary in ('짧음', '가' * 251, None):
        summary = match_timeline_summary({'summary': bad_summary}, summary_inputs, [])
        assert summary['summary_status'] == '생략' and summary['lead'] is None and summary['summary_attempts'] == 1
    # /api/chat JSON 파싱부터 월 문장·요약 검사까지 통합 경로를 확인한다. 실패해도 재호출하지 않는다.
    context = '계좌 거래 내역'
    grounded_lead = '강선우 공천헌금 사건의 주요 단서는 계좌 거래 내역을 추적하면서 확보한 것으로 보도됐다.'
    combined_event = dict(event, date_from='2026-01-30', date_to='2026-03-01', summary=grounded_lead,
                          representative=dict(event['representative'], title=' '.join(
                              [summary_inputs[0]['title'], *summary_inputs[0]['other_titles']])))
    combined_result = dict(result, events=[combined_event])
    for summary_text, status in ((grounded_lead, '통과'), ('짧음', '생략')):
        combined = {'lead': summary_text, 'context': context}

        def combined_http(request, timeout):
            payload = json.loads(request.data)
            assert request.full_url.endswith('/api/chat')
            assert set(payload['format']['properties']) == {'lead', 'context'}
            assert 'num_predict' in payload['options'] or llm_think()
            summary_input = json.loads(payload['messages'][1]['content'])
            assert summary_input['events'] == [f'{combined_event["representative"]["title"]} — {grounded_lead}']
            assert 'flow' not in summary_input
            assert '날짜·기사 번호·인용은 쓰지 않는다' in payload['messages'][0]['content']
            return BytesIO(json.dumps({'message': {'content': json.dumps(combined)}}).encode())

        with patch.dict(globals(), _timeline_cache=OrderedDict(), timeline_centers=lambda _e: {},
                        timeline_keywords=lambda *_a: {}, keyword_timeline_candidates=lambda _p: [combined_event]), \
                patch(__name__ + '.urlopen', side_effect=combined_http) as http:
            combined_timeline = generate_timeline(combined_result)
        assert http.call_count == combined_timeline['llm_calls'] == 1
        assert combined_timeline['summary_status'] == status and combined_timeline['summary_attempts'] == 1
        assert combined_timeline['months'][0]['text'] == clean_month_title(combined_event['representative']['title'])
        assert combined_timeline['months'][0]['source'] == '제목'
    sql, values = keyword_timeline_query({'keywords': ['강선우', '공천헌금'], 'q': ''}, None)
    assert 'INTERSECT' in sql and 'GROUP BY grouped, group_id' in sql
    assert 'ORDER BY publisher_count DESC, article_count DESC' in sql and 'article_embeddings' not in sql
    assert 'count(*) AS matched_count' in sql and 'FROM article_groups g' in sql
    assert 'CASE WHEN grouped THEN totals.article_count ELSE 1 END AS article_count' in sql
    assert values == ('강선우', '공천헌금', 50)
    sql, values = keyword_timeline_query({'keywords': ['트럼프'], 'q': '트럼프'}, '[1]')
    assert 'max(score)' in sql and 'OFFSET 0' in sql and values == ('트럼프', '[1]', 50, 12)
    assert 'ORDER BY score DESC NULLS LAST, article_count DESC' in ' '.join(sql.split())
    assert 'similarity_rank <= %s OR coverage_rank <= %s' in sql
    filter_events = [dict(event, group_id=i) for i in range(10)]
    filter_centers = {(e.get('grouped', True), e['group_id']): [i / 10, math.sqrt(1 - (i / 10) ** 2)]
                      for i, e in enumerate(filter_events)}
    assert [e['group_id'] for e in filter_timeline_by_query(filter_events, filter_centers, '[1,0]')] == [9]
    assert [e['group_id'] for e in filter_timeline_by_query(filter_events, filter_centers, '[0,1]')] == list(range(6))
    assert filter_timeline_by_query(filter_events, {}, '[1,0]') == []
    assert filter_timeline_by_query(filter_events, filter_centers, '[1,0]', .91) == []
    assert len(filter_timeline_by_query(filter_events, {(True, 0): [.83, math.sqrt(1 - .83 ** 2)]},
                                        '[1,0]')) == 1
    with patch.dict(os.environ, TIMELINE_Q_MIN_COS='0.91'):
        assert timeline_query_min_cos() == .91
    for invalid in ('nan', 'inf', '1.1', 'bad'):
        with patch.dict(os.environ, TIMELINE_Q_MIN_COS=invalid):
            try:
                timeline_query_min_cos()
            except SearchError:
                pass
            else:
                raise AssertionError('잘못된 코사인 기준 허용')
    combined_result = dict(result, params=dict(keywords=['강선우'], q='주제', limit=20))
    candidates = [dict(event, group_id=i) for i in range(4)]
    vectors = {(True, i): ([1, 0] if i < 2 else [0, 1]) for i in range(4)}
    with patch.dict(globals(), _timeline_cache=OrderedDict(),
                    keyword_timeline_candidates=lambda _p: candidates,
                    timeline_representative_vectors=lambda _es: vectors,
                    timeline_centers=lambda _es: {}, timeline_keywords=lambda *_a: {},
                    query_embedding=lambda _q: '[1,0]',
                    ollama_chat=lambda _s, _d, schema, _deadline, **_kw: (
                        {str(i): 2 for i in range(1, 5)} if '1' in schema['properties'] else
                        {'months': [], 'summary': ''}, 1)):
        relaxed = generate_timeline(combined_result)
        assert relaxed['filter_relaxed'] and relaxed['filtered_candidate_count'] == 2
        assert relaxed['candidate_count'] == 4 and len(relaxed['items']) == 4
        assert generate_timeline(combined_result)['cached']
    with patch.dict(globals(), _timeline_cache=OrderedDict(),
                    keyword_timeline_candidates=lambda _p: [],
                    timeline_representative_vectors=lambda _es: {}, query_embedding=lambda _q: '[1,0]'), \
            patch(__name__ + '.ollama_chat') as llm_mock:
        empty = generate_timeline(combined_result)
        assert empty['items'] == [] and empty['filter_relaxed'] and not llm_mock.called
    assert timeline_candidates(dict(events=[], timeline_candidates=many)) == many
    # 새 후보는 결과 상위 목록에 없는 사건도 순위 조회 오류 없이 선택한다.
    assert select_timeline_events(dict(events=[], timeline_candidates=[event]), [event]) == [event]
    # 일치 기사 1건인 큰 묶음이 일치 기사 5건인 작은 묶음보다 선택된다.
    large = dict(event, group_id=689977, article_count=28, matched_count=1)
    small = [dict(event, group_id=i, article_count=5, matched_count=5) for i in range(11)]
    volume_candidates = [large, *small]
    selected = select_timeline_events(dict(events=[], timeline_candidates=volume_candidates), volume_candidates)
    assert len(selected) == TIMELINE_LIMIT and large in selected and small[-1] not in selected
    # SQL 상위 후보의 묶음·단독 기사 모두 기존 사건 형식으로 복원한다.
    connection = MagicMock()
    connection.__enter__.return_value = connection
    grouped_row = dict(group_id=10, grouped=True, article_count=28, matched_count=1, score=None,
                       date_from='2026-01-02', date_to='2026-01-02', latest_match='2026-01-02',
                       matches=[dict(id=11, score=None)])
    single_row = dict(grouped_row, group_id=99, grouped=False, article_count=1, matched_count=1,
                      matches=[dict(id=99, score=None)])
    def detail(ident):
        return dict(id=ident, title=f'기사 {ident}', published_date='2026-01-02',
                    url='', publisher='동아일보', categories=[])
    connection.execute.side_effect = [MagicMock(), MagicMock(),
        MagicMock(**{'fetchall.return_value': [grouped_row, single_row]}),
        MagicMock(**{'fetchall.return_value': [dict(group_id=10, date_from='2026-01-01',
                    date_to='2026-01-03', representative_id=10, publishers=['동아일보'])]}),
        MagicMock(**{'fetchall.return_value': [detail(10), detail(11), detail(99)]})]
    with patch.object(psycopg, 'connect', return_value=connection) as connect, patch.dict(
            globals(), connection_options=lambda _p: {}, article_duplicate_ids=lambda *_args: []):
        candidates = keyword_timeline_candidates({'keywords': ['강선우'], 'q': ''})
    assert connection.read_only and connection.execute.call_count == 5
    assert connect.call_args.kwargs['options'] == '-c default_transaction_read_only=on'
    assert 'REPEATABLE READ READ ONLY' in connection.execute.call_args_list[0].args[0]
    assert candidates[0]['article_count'] == 28 and candidates[0]['matched_count'] == 1
    assert candidates[0]['representative']['id'] == 10
    assert candidates[0]['date_from'] == '2026-01-01' and candidates[0]['matches'][0]['id'] == 11
    assert not candidates[1]['grouped'] and candidates[1]['representative']['id'] == 99
    connection.reset_mock()
    connection.execute.side_effect = [MagicMock(), MagicMock(**{'fetchall.return_value': [
        {'article_id': 10, 'vector': '[3,4]'}]})]
    with patch.object(psycopg, 'connect', return_value=connection) as connect, patch.dict(
            globals(), connection_options=lambda _p: {}):
        vectors = timeline_representative_vectors(candidates)
    assert connection.read_only and connect.call_args.kwargs['options'] == '-c default_transaction_read_only=on'
    assert connection.execute.call_args.args[1] == ([10, 99],)
    assert vectors == {(True, 10): [.6, .8], (False, 99): None}
    with patch.object(psycopg, 'connect') as connect:
        assert timeline_representative_vectors([]) == {} and not connect.called
    cache = OrderedDict()
    for i in range(101):
        _cache_put(cache, i, {'i': i})
    assert len(cache) == 100 and _cache_get(cache, 0) is None
    with patch.dict(globals(), convert_question=lambda _a: (_ for _ in ()).throw(LLMUnavailable('로컬 AI 연결 안 됨'))):
        try:
            search({'ask': '질문'})
            assert False, '질문 변환 실패를 알려야 함'
        except QuestionConversionError:
            pass
    assert validate_params({"keywords": "강선우, 공천헌금 강선우\n"})["keywords"] == ["강선우", "공천헌금"]
    assert validate_params({"q": "주제 문장"})["mode"] == "topic"
    assert validate_params({"mode": "keyword", "q": "a,b"})["keywords"] == ["a", "b"]
    assert validate_params({"keywords": ["a", "b"], "q": "주제"})["mode"] == "combined"
    assert validate_params({"keywords": "러브버그"})["limit"] == 20
    assert search({})['events'] == [] and search({"keywords": ", ,", "q": " "})['candidate_count'] == 0
    for bad in ([], {"keywords": [1]}, {"keywords": None}, {"q": None},
                {"q": "a", "mode": "hybrid"}, {"q": "a", "limit": True},
                {"q": "a", "limit": 51}, {"q": "a", "publishers": []}, {"q": "x" * 201},
                {"keywords": [str(i) for i in range(31)]}, {"keywords": "x" * 101}):
        try:
            validate_params(bad)
        except SearchError:
            pass
        else:
            raise AssertionError("잘못된 입력을 허용함")
    rows = [dict(id=i, group_id=g, score=score, published_date=d) for i, g, score, d in (
        (1, 1, .99, "2026-10-01"), (2, 2, .85, "2026-10-02"),
        (3, 3, .90, "2026-10-02"), (4, 4, .849, "2026-10-03"))]
    for mode in ("topic", "combined"):
        eligible = [r for r in rows if r["score"] >= result_min_cos()]
        events = fold_events(eligible, mode)
        for event in events:
            event["article_count"] = 1
        assert [e["group_id"] for e in sort_result_events(events, mode)] == [3, 2, 1]
        assert len(rows) - len(eligible) == 1
    events = fold_events(rows, "keyword")
    for event in events:
        event["article_count"] = 2 if event["group_id"] == 2 else 1
    assert [e["group_id"] for e in sort_result_events(events, "keyword")] == [4, 2, 3, 1]
    assert fold_events([], "keyword") == []
    for invalid in ("oops", "nan", "inf", "1.1", "-1.1", ""):
        with patch.dict(os.environ, RESULT_MIN_COS=invalid):
            assert result_min_cos() == .85
    with patch.dict(os.environ, RESULT_MIN_COS="0.9"):
        assert result_min_cos() == .9
    sql, values = candidate_query(validate_params({"keywords": "a,b"}), None)
    assert "INTERSECT" in sql and "embedding" not in sql and values[:2] == ("a", "b")
    assert "AS partial_match" in sql and sql.count("%s") == len(values) and values[-1] == ROUTINE_TITLE
    sql, values = candidate_query(validate_params({"mode": "topic", "q": "주제"}), "[]")
    assert "ORDER BY distance LIMIT" in sql and values[:2] == ("[]", 200)
    assert sql.count("%s") == len(values) and values[-1] == ROUTINE_TITLE
    sql, values = candidate_query(validate_params({"keywords": "a,b", "q": "주제"}), "[]", [1, 2])
    assert "eligible AS MATERIALIZED" in sql and "OFFSET 0" in sql
    assert "ORDER BY e.embedding <=> %s::vector" in sql and values[:4] == ([1, 2], "[]", "[]", 200)
    assert "AS partial_match" in sql and sql.count("%s") == len(values) and values[-1] == ROUTINE_TITLE
    sql, values = stats_query(["a", "b"])
    assert sql.count("keyword=%s") == 2 and values[:2] == ("a", "b")
    assert "matched AS MATERIALIZED" in sql and "sum(k.mention_count)" in sql
    assert "NOT (keyword=ANY" in sql and set(values[-1]) == KEYWORD_STOPWORDS | {"a", "b"}
    assert "article_count >= 3 AND lift > 1" in sql and "mention_sum * ln(lift) DESC" in sql
    assert "PARTITION BY keyword" in sql and "sum(mention_sum) OVER ()" in sql
    assert validate_params({"q": "x" * 200, "keywords": "y" * 100})["mode"] == "combined"
    assert "generate_series(LEAST('2026-01-01'" in sql and "COALESCE(c.count, 0)" in sql
    assert "GROUP BY d.month, k.keyword" in sql and "PARTITION BY month" in sql and "rank <= 4" in sql
    sql, values = stats_query([], [1, 2])
    assert "INTERSECT" not in sql and values == ([1, 2], sorted(KEYWORD_STOPWORDS))
    # SQL 결과의 공개 통계 형식·집합 기준·검색 ID 재사용을 DB 없이 검증한다.
    from unittest.mock import Mock
    # 실제 검색 경로에서 사건 수와 무관하게 유사도 하한을 유지하는지 확인한다.
    mock_rows = [dict(id=i, group_id=None, score=score, published_date=day,
                      ranked_candidate_count=4) for i, score, day in (
        (1, .9, '2026-01-01'), (2, .86, '2026-01-02'),
        (3, .84, '2026-01-03'), (4, .8, '2026-01-03'))]
    mock_conn = Mock()
    def fake_execute(sql, values):
        response = Mock()
        if 'FROM articles a LEFT JOIN article_sources' in sql:
            response.fetchall.return_value = [dict(id=i, title=str(i), published_date='2026-01-01',
                url='', publisher='', categories=[]) for i in values[0]]
        elif 'WITH members AS' in sql:
            response.fetchall.return_value = [dict(id=i, group_id=i, grouped=False, title=str(i)) for i in values[1]]
        else:
            response.fetchall.return_value = mock_rows
        return response
    mock_conn.execute.side_effect = fake_execute
    with patch.dict(globals(), find_core_articles=lambda _conn, params, *_: {1: True} if params['keywords'] else {},
                    collect_core_stats=lambda *_: dict(basis='', monthly=[]),
                    keyword_candidate_ids=lambda *_: [1, 2, 3, 4], related_article_ids=lambda *_: []):
        combined = validate_params({'keywords': 'a', 'q': '주제'})
        result = _search_db(mock_conn, combined, '[]')
        assert not result['result_filter_relaxed'] and result['excluded_candidate_count'] == 2
        assert [event['group_id'] for event in result['events']] == [1]
        assert [event['group_id'] for event in result['related_events']] == [2]
        mock_rows[2]['score'] = .85
        result = _search_db(mock_conn, combined, '[]')
        assert not result['result_filter_relaxed'] and len(result['events'] + result['related_events']) == 3
        assert result['excluded_candidate_count'] == 1
        for row in mock_rows:
            row['score'] = .8
        result = _search_db(mock_conn, validate_params({'q': '주제'}), '[]')
        assert not result['result_filter_relaxed'] and not result['events']
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "article_ids": [1, 2],
        "monthly": [{"month": "2026-01", "count": 0, "top_keywords": []},
                    {"month": "2026-02", "count": 2,
                     "top_keywords": [{"keyword": "정치", "mention_sum": 7,
                                       "article_count": 3, "lift": 1.5}]}],
    }
    stats, ids = collect_stats(conn, validate_params({"keywords": "a", "q": "주제"}))
    assert ids == [1, 2] and len(ids) == sum(m["count"] for m in stats["monthly"])
    assert set(stats) == {"basis", "monthly"}
    assert stats["monthly"][1]["top_keywords"][0] == dict(keyword="정치", mention_sum=7, article_count=3, lift=1.5)
    assert "주제(q)" in stats["basis"] and conn.execute.call_count == 1
    assert "HNSW" in collect_stats(conn, validate_params({"q": "주제"}), ids)[0]["basis"]
    assert search({})["stats"] == {"basis": "검색 조건 없음", "monthly": []}
    sql, values = candidate_query(validate_params({"keywords": "a"}), None, ids)
    assert "unnest(%s::bigint[])" in sql and values[0] == ids
    assert "AS partial_match" in sql and sql.count("%s") == len(values) and values[-1] == ROUTINE_TITLE
    # 준비 중 요청의 대기·중복 로드 방지·실패 안내를 실제 모델 없이 확인한다.
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch

    entered, release, requested = Event(), Event(), Event()
    calls = []

    def fake_encode(q):
        calls.append(q)
        if q == "검색 모델 준비":
            entered.set()
            assert release.wait(5)
        return "[1]"

    def request():
        requested.set()
        return query_embedding("주제")

    with patch.dict(globals(), _warmup_thread=None, _warmup_done=Event(),
                    _warmup_failed=False, _encode_query=fake_encode):
        worker = start_model_warmup()
        assert entered.wait(5) and start_model_warmup() is worker
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(request)
            try:
                assert requested.wait(5) and not pending.done()
            finally:
                release.set()
            assert pending.result(timeout=5) == "[1]"
        worker.join(timeout=5)
        assert not worker.is_alive() and calls == ["검색 모델 준비", "주제"]

    def fail_encode(_q):
        raise RuntimeError("비공개 원본 오류")

    with patch.dict(globals(), _warmup_thread=None, _warmup_done=Event(),
                    _warmup_failed=False, _encode_query=fail_encode):
        start_model_warmup().join(timeout=5)
        try:
            query_embedding("주제")
        except SearchError as exc:
            assert "사전 로드 실패" in str(exc) and "비공개" not in str(exc)
        else:
            raise AssertionError("준비 실패를 표시하지 않음")
