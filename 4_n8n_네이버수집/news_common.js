// build_workflow.js가 Code 노드 앞에 삽입하는 공통 코드. 외부 npm 의존성 없음.
const MAX_PAGES_PER_RUN = 1000; // robots 제외 원문 HTTP 시도 수. 리다이렉트도 예산을 소비한다.
const pad = (n, w = 2) => String(n).padStart(w, '0');
const kst = d => {
  const k = new Date(d.getTime() + 9 * 3600e3);
  return `${k.getUTCFullYear()}-${pad(k.getUTCMonth() + 1)}-${pad(k.getUTCDate())}T${pad(k.getUTCHours())}:${pad(k.getUTCMinutes())}:${pad(k.getUTCSeconds())}+09:00`;
};
const HTML_ENTITIES = {
  middot: '·', hellip: '…', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”', laquo: '«', raquo: '»',
  ndash: '–', mdash: '—', nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'",
  bull: '•', times: '×', copy: '©', reg: '®', trade: '™', ensp: ' ', emsp: ' ', thinsp: ' ',
};
function decodeEntities(value) {
  // 제목/본문에 공통 적용. 미지원 named entity는 영단어로 세지 않도록 공백으로 바꾼다.
  return String(value || '').replace(/&(#x[\da-f]+|#\d+|[a-z][a-z\d]*);/gi, (_, entity) => {
    if (entity[0] !== '#') return Object.hasOwn(HTML_ENTITIES, entity.toLowerCase()) ? HTML_ENTITIES[entity.toLowerCase()] : ' ';
    const hex = entity[1].toLowerCase() === 'x';
    const n = parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10);
    return n >= 32 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : ' ';
  });
}
const clean = s => decodeEntities(String(s || '').replace(/<[^>]*>/g, ' ')).replace(/\s+/g, ' ').trim();
const articleTitleKey = title => title.normalize('NFC').replace(/[\s\u0085]+/g, ' ').trim();
const isYnaPhoto = url => /^https?:\/\/(?:[a-z\d-]+\.)*yna\.co\.kr(?::\d+)?\/view\/PYH/i.test(url || '');
const JOSA = ['에서는', '으로는', '에게서', '이라는', '에서', '으로', '에게', '까지', '부터', '보다', '처럼', '만큼',
  '이나', '라는', '이란', '은', '는', '이', '가', '을', '를', '의', '에', '와', '과', '도', '만', '로'];
const STOPWORDS = new Set(['단독', '속보', '종합', '포토', '사진', '영상', '기자', '오늘', '내일', '어제', '위해', '대한',
  '관련', '있다', '없다', '했다', '한다', '된다', '이번', '올해', '지난', '최근', '가운데', '이후', '통해', '대해', '것',
  '있는', '없는', '이들', '그들', '함께', '관심', '따라', '따른', '이날', '지난해', '내년', '당시', '현재', '경우',
  '오전', '오후', '것으로', '것을', '것이', '것은', '등을', '등이', '등은', '등의', '등과', '등도', '등',
  '이를', '이에', '이와', '이런', '이러한', '그런', '그러한', '또한', '그리고', '그러나', '하지만', '다만',
  '때문', '때문에', '대신', '한편', '앞서', '아울러', '즉', '및', '또', '수', '때', '중', '이상', '이하',
  '바로', '보다', '모두', '계속', '여러', '어떤', '다른', '같은', '가장', '매우', '다시', '먼저', '이제',
  '대부분', '정도', '여기', '여기에', '거기', '자신', '우리', '이번에', '이같이', '이같은', '이같다',
  '무단전재', '재배포', '전재', '금지', '저작권', '특파원', '논설위원', '편집국']);
const VERBAL_WORDS = new Set(['하는', '되는', '위한', '통한', '인한', '한', '하고', '하며', '하면', '하던', '했던',
  '있으며', '없으며', '있고', '없고', '있었던', '없었던', '되며', '되어', '되고', '됐고', '됐다', '되었다',
  '이었다', '이다', '입니다', '것이다', '것이라고', '것이며', '한다고', '했다고', '한다는', '했다는',
  '밝혔다', '말했다', '전했다', '설명했다', '강조했다', '덧붙였다', '보도했다', '나타났다', '알려졌다']);
// 짧은 용언만 탈락시키며 북한·권한 같은 명사가 -한 규칙에 잘리지 않게 한다.
const ENDING_NOUNS = new Set(['북한', '남한', '우한', '권한', '제한', '기한', '서한']);
const newsFunctionWord = w => STOPWORDS.has(w) || VERBAL_WORDS.has(w) ||
  (!ENDING_NOUNS.has(w) && ['는', '한', '했다', '된다', '이다'].some(e => w.endsWith(e) && w.length - e.length < 2));
const stripJosa = w => {
  for (const j of JOSA) if (w.endsWith(j) && w.length - j.length >= 2) return w.slice(0, -j.length);
  return w;
};
function keywordsOf(text) {
  const counts = new Map();
  decodeEntities(text).replace(/\[[^\]]*\]|【[^】]*】/g, ' ').split(/[^가-힣A-Za-z0-9]+/)
    // 조사 제거 전후 모두 검사해야 '밝혔다'→'밝혔', '관련' 같은 잔여 토큰이 남지 않는다.
    .filter(w => !newsFunctionWord(w)).map(stripJosa)
    .filter(w => w.length >= 2 && !/^\d+$/.test(w) && !newsFunctionWord(w))
    .forEach(w => counts.set(w, (counts.get(w) || 0) + 1));
  // Map 삽입 순서 + stable sort: 횟수 내림차순, 동률이면 제목→본문 첫 출현 순서.
  return Object.fromEntries([...counts].sort((a, b) => b[1] - a[1]).slice(0, 30));
}

// ---- 언론사 도메인 → 언론사명 (없으면 도메인 그대로 둔다) ----
const PUBLISHER_DATA = {
  "domains": {
    "sports.donga.com": "스포츠동아",
    "donga.com": "동아일보",
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보",
    "kmib.co.kr": "국민일보",
    "segye.com": "세계일보",
    "seoul.co.kr": "서울신문",
    "munhwa.com": "문화일보",
    "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제",
    "mt.co.kr": "머니투데이",
    "sedaily.com": "서울경제",
    "edaily.co.kr": "이데일리",
    "asiae.co.kr": "아시아경제",
    "fnnews.com": "파이낸셜뉴스",
    "heraldcorp.com": "헤럴드경제",
    "yna.co.kr": "연합뉴스",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "sbs.co.kr": "SBS",
    "ytn.co.kr": "YTN",
    "jtbc.co.kr": "JTBC",
    "ichannela.com": "채널A",
    "mbn.co.kr": "MBN",
    "tvchosun.com": "TV조선",
    "nocutnews.co.kr": "노컷뉴스",
    "ohmynews.com": "오마이뉴스",
    "pressian.com": "프레시안",
    "zdnet.co.kr": "지디넷코리아",
    "etnews.com": "전자신문",
    "dt.co.kr": "디지털타임스",
    "bloter.net": "블로터",
    "inews24.com": "아이뉴스24",
    "gukjenews.com": "국제뉴스",
    "ajunews.com": "아주경제",
    "newspim.com": "뉴스핌",
    "hansbiz.co.kr": "한스경제",
    "etoday.co.kr": "이투데이",
    "topstarnews.net": "톱스타뉴스",
    "m-i.kr": "매일일보",
    "breaknews.com": "브레이크뉴스",
    "dailian.co.kr": "데일리안",
    "shinailbo.co.kr": "신아일보",
    "osen.co.kr": "OSEN",
    "yonhapnewstv.co.kr": "연합뉴스TV",
    "viva100.com": "브릿지경제",
    "g-enews.com": "글로벌이코노믹",
    "enewstoday.co.kr": "이뉴스투데이",
    "wikitree.co.kr": "위키트리",
    "tf.co.kr": "더팩트",
    "pinpointnews.co.kr": "핀포인트뉴스",
    "imaeil.com": "매일신문",
    "ggilbo.com": "금강일보",
    "newscj.com": "천지일보",
    "dynews.co.kr": "동양일보",
    "daily.hankooki.com": "데일리한국",
    "bntnews.co.kr": "bnt뉴스",
    "vegannews.co.kr": "비건뉴스",
    "sportsseoul.com": "스포츠서울",
    "newsworker.co.kr": "뉴스워커",
    "newdaily.co.kr": "뉴데일리",
    "metroseoul.co.kr": "메트로신문",
    "kukinews.com": "쿠키뉴스",
    "xportsnews.com": "엑스포츠뉴스",
    "asiatoday.co.kr": "아시아투데이",
    "kado.net": "강원도민일보",
    "slist.kr": "싱글리스트",
    "ekn.kr": "에너지경제신문",
    "job-post.co.kr": "잡포스트",
    "mydaily.co.kr": "마이데일리",
    "jnilbo.com": "전남일보",
    "bbsi.co.kr": "BBS불교방송",
    "kyeonggi.com": "경기일보",
    "kwnews.co.kr": "강원일보",
    "ccdn.co.kr": "충청매일",
    "mdtoday.co.kr": "메디컬투데이",
    "einfomax.co.kr": "연합인포맥스",
    "busan.com": "부산일보",
    "sentv.co.kr": "서울경제TV",
    "dnews.co.kr": "대한경제",
    "starnewskorea.com": "스타뉴스",
    "sportschosun.com": "스포츠조선",
    "cbci.co.kr": "CBC뉴스",
    "thepublic.kr": "더퍼블릭",
    "tokenpost.kr": "토큰포스트",
    "cctimes.kr": "충청타임즈",
    "newsen.com": "뉴스엔",
    "sidae.com": "시대",
    "wowtv.co.kr": "한국경제TV",
    "cnbnews.com": "CNB뉴스",
    "insight.co.kr": "인사이트",
    "coinreaders.com": "코인리더스",
    "kbmaeil.com": "경북매일",
    "financialpost.co.kr": "파이낸셜포스트",
    "spotvnews.co.kr": "스포티비뉴스",
    "hidomin.com": "경북도민일보",
    "newsclaim.co.kr": "뉴스클레임",
    "kyongbuk.co.kr": "경북일보",
    "jemin.com": "제민일보",
    "asiatime.co.kr": "아시아타임즈",
    "newsworks.co.kr": "뉴스웍스",
    "megaeconomy.co.kr": "메가경제",
    "tenasia.co.kr": "텐아시아",
    "daejonilbo.com": "대전일보",
    "chungnamilbo.co.kr": "충남일보",
    "digitaltoday.co.kr": "디지털투데이",
    "jeonmae.co.kr": "전국매일신문",
    "news2day.co.kr": "뉴스투데이",
    "kookje.co.kr": "국제신문",
    "newsfreezone.co.kr": "뉴스프리존",
    "econovill.com": "이코노믹리뷰",
    "idaegu.co.kr": "대구신문",
    "sjbnews.com": "새전북신문",
    "economist.co.kr": "이코노미스트",
    "newsprime.co.kr": "프라임경제",
    "weekly.hankooki.com": "주간한국",
    "fntoday.co.kr": "파이낸스투데이",
    "ppss.kr": "ㅍㅍㅅㅅ",
    "kihoilbo.co.kr": "기호일보",
    "ddaily.co.kr": "디지털데일리",
    "kgnews.co.kr": "경기신문",
    "isplus.com": "일간스포츠",
    "sportsworldi.com": "스포츠월드",
    "sisajournal.com": "시사저널",
    "lecturernews.com": "한국강사신문",
    "businesspost.co.kr": "비즈니스포스트",
    "naeil.com": "내일신문",
    "tvreport.co.kr": "TV리포트",
    "medipana.com": "메디파나뉴스",
    "newstomato.com": "뉴스토마토",
    "joseilbo.com": "조세일보",
    "mtn.co.kr": "머니투데이방송",
    "ccdailynews.com": "충청일보",
    "joongdo.co.kr": "중도일보",
    "discoverynews.kr": "디스커버리뉴스",
    "idomin.com": "경남도민일보",
    "mediapen.com": "미디어펜",
    "ebn.co.kr": "EBN",
    "jeollailbo.com": "전라일보",
    "namdonews.com": "남도일보",
    "unn.net": "한국대학신문",
    "ksilbo.co.kr": "경상일보",
    "obsnews.co.kr": "OBS경인TV",
    "nongmin.com": "농민신문",
    "pennmike.com": "펜앤드마이크",
    "inews365.com": "충북일보",
    "segyebiz.com": "세계비즈",
    "incheonilbo.com": "인천일보",
    "gnmaeil.com": "경남매일",
    "businesskorea.co.kr": "비즈니스코리아",
    "polinews.co.kr": "폴리뉴스",
    "industrynews.co.kr": "인더스트리뉴스",
    "joongangenews.com": "중앙이코노미뉴스",
    "gokorea.kr": "공감신문",
    "newstnt.com": "뉴스티앤티",
    "cnbizm.com": "문화경제",
    "kbsm.net": "경북신문",
    "cstimes.com": "컨슈머타임스",
    "dkilbo.com": "대경일보",
    "beyondpost.co.kr": "비욘드포스트",
    "getnews.co.kr": "글로벌경제신문",
    "kmaeil.com": "경인매일",
    "ngonews.kr": "한국NGO신문",
    "pointdaily.co.kr": "포인트데일리",
    "jasm.co.kr": "중앙신문",
    "thefairnews.co.kr": "더페어",
    "widedaily.com": "와이드경제",
    "ziksir.com": "직썰",
    "newstown.co.kr": "뉴스타운",
    "lawissue.co.kr": "로이슈",
    "finomy.com": "현대경제신문",
    "tournews21.com": "투어코리아",
    "sateconomy.co.kr": "토요경제",
    "nc.press": "뉴스컬처",
    "koreadaily.com": "미주중앙일보",
    "e-science.co.kr": "이코노미사이언스",
    "kfenews.co.kr": "한국금융경제신문",
    "mediafine.co.kr": "미디어파인",
    "newsroad.co.kr": "뉴스로드",
    "4th.kr": "포쓰저널",
    "ikbc.co.kr": "KBC광주방송",
    "pointe.co.kr": "포인트경제",
    "m-economynews.com": "M이코노미뉴스",
    "smartbizn.com": "스마트비즈",
    "ccreview.co.kr": "충청리뷰",
    "goodmorningcc.com": "굿모닝충청",
    "thefirstmedia.net": "더퍼스트미디어",
    "ulsanpress.net": "울산신문"
  },
  "oids": {
    "001": "연합뉴스",
    "009": "매일경제",
    "015": "한국경제",
    "020": "동아일보",
    "023": "조선일보",
    "025": "중앙일보",
    "028": "한겨레",
    "032": "경향신문"
  }
};
const PUBLISHERS = PUBLISHER_DATA.domains;

// n8n 작업 실행기(runner)에는 URL 객체가 없어 정규식으로 도메인을 뽑는다.
const host = url => ((String(url).match(/^https?:\/\/([^/:?#]+)/i) || [])[1] || '').toLowerCase().replace(/^(www|m|news|biz|n)\./, '');
const publisherOf = url => {
  const naver = String(url).match(/^https?:\/\/n\.news\.naver\.com\/mnews\/article\/(\d{3})\/[^/?#]+/i);
  if (naver && Object.hasOwn(PUBLISHER_DATA.oids, naver[1])) return PUBLISHER_DATA.oids[naver[1]];
  const h = host(url);
  const key = Object.keys(PUBLISHERS).filter(d => h === d || h.endsWith('.' + d)).sort((a, b) => b.length - a.length)[0];
  return key ? PUBLISHERS[key] : h;
};

// 기사 분류와 기자명 후보 검증이 같은 분류 사전을 사용한다.
// BigKinds 분류. 검색어로 1순위를 정한 뒤 제목 판정어로 나머지 순위를 채운다.
const CATEGORIES = {
  '정치': {
    '국회_정당': ['국회', '여당', '야당', '민주당', '국민의힘', '의원', '원내', '당대표', '탄핵', '법안', '국정감사'],
    '청와대': ['청와대', '대통령', '대통령실', '용산', '영부인', '비서실'],
    '선거': ['선거', '총선', '대선', '공천', '선관위', '투표', '당선'],
    '외교': ['국방', '외교', '합참', '방위', '주한미군', '외교부', '정상회담', '동맹'],
    '행정_자치': ['총리', '장관', '정부', '행안부', '지자체', '도지사', '감사원'],
    '북한': ['북한', '김정은', '평양', '북미', '남북', '탈북', '미사일'],
    '정치일반': [],
  },
  '경제': {
    '증권_증시': ['증시', '코스피', '코스닥', '주가', '상장', '공모', '증권', '외국인 순매수', '펀드', 'ETF'],
    '유통': ['유통', '편의점', '마트'],
    '금융_재테크': ['금리', '은행', '대출', '한은', '한국은행', '금융', '보험', '카드사', '예금', '가계부채'],
    '부동산': ['부동산', '아파트', '전세', '월세', '분양', '재건축', '집값', '청약', '주택'],
    '자동차': ['자동차', '현대차', '신차', '시승', '전기차', 'SUV'],
    '경제일반': ['물가', '가격'],
    '산업_기업': ['삼성', 'SK', 'LG', '재계', '그룹', '배터리', '조선', '철강', '중소기업', '소상공인'],
    '반도체': ['반도체', '파운드리', 'HBM'],
    '취업_창업': ['취업', '창업', '벤처', '스타트업', '투자유치'],
    '서비스_쇼핑': ['소비자', '할인', '쇼핑'],
    '자원': ['유가', '원자재'],
    '무역': ['관세', '무역', '수출'],
    '국제경제': ['연준', '국제경제'],
    '외환': ['환율', '달러', '외환'],
  },
  '사회': {
    '사건_사고': ['사고', '화재', '사망', '경찰', '검찰', '법원', '구속', '체포', '살인', '폭행', '사기', '재판', '판결', '혐의'],
    '교육_시험': ['교육', '학교', '학생', '교사', '수능', '대학', '입시', '교육부'],
    '사회일반': ['도로', '교통', '지하철', '버스', 'KTX', '항공편', '운행'],
    '노동_복지': ['노동', '노조', '파업', '임금', '근로', '고용', '일자리', '최저임금', '복지', '인권', '노인', '저출생', '출산', '돌봄', '연금'],
    '의료_건강': ['병원', '의사', '의료', '의대', '식품', '감염', '백신', '환자', '보건', '건강', '다이어트', '운동법', '수면', '질환'],
    '여성': ['여성', '성평등'],
    '날씨': ['날씨', '기온', '태풍', '한파', '기상청', '폭염'],
    '미디어': ['언론', '방송', '기자', '방통위'],
    '장애인': ['장애인', '이동권'],
    '환경': ['환경', '기후', '탄소', '미세먼지', '오염', '재활용'],
  },
  '국제': {
    '미국_북미': ['미국', '트럼프', '백악관', '워싱턴', '캐나다'],
    '중동_아프리카': ['중동', '이스라엘', '가자', '하마스', '이란', '사우디', '아프리카'],
    '유럽_EU': ['유럽', 'EU', '영국', '프랑스', '독일', '우크라이나', '나토'],
    '중국': ['중국', '시진핑', '대만', '홍콩'],
    '일본': ['일본', '도쿄'],
    '아시아': ['인도', '베트남', '호주', '아세안'],
    '러시아': ['러시아', '푸틴'],
    '중남미': ['브라질', '멕시코', '중남미'],
    '국제일반': [],
  },
  '문화': {
    '방송_연예': ['연예', '드라마', '예능', 'OTT'],
    '전시_공연': ['공연', '전시', '뮤지컬', '콘서트'],
    '출판': ['출판', '출간', '신간', '작가', '소설'],
    '영화': ['영화'],
    '요리_여행': ['여행', '관광', '축제', '레저', '캠핑', '호텔', '맛집', '음식', '요리', '레시피', '카페'],
    '미술_건축': ['미술', '건축'],
    '종교': ['종교', '교회', '사찰', '성당', '불교', '천주교'],
    '문화일반': [],
    '생활': ['패션', '뷰티', '화장품', '브랜드'],
    '학술_문화재': ['학술', '문화재', '유적'],
    '음악': ['음악', '가요', '클래식'],
  },
  'IT_과학': {
    '과학': ['과학', '우주', '연구', '위성', '로봇', '바이오'],
    '모바일': ['스마트폰', '갤럭시', '아이폰', '모바일', '통신', '5G', '6G', 'SKT', 'KT', 'LG유플러스'],
    '보안': ['보안', '해킹', '개인정보', '유출', '랜섬웨어'],
    '콘텐츠': ['게임', 'e스포츠', '넥슨', '엔씨', '크래프톤'],
    'IT_과학일반': ['AI', '인공지능', '챗GPT', '빅테크', '소프트웨어', '데이터센터', '클라우드', '컴퓨터', 'PC', '노트북', 'GPU', '엔비디아'],
    '인터넷_SNS': ['인터넷', 'SNS', '유튜브', '네이버', '카카오', '플랫폼', '메타', '구글'],
  },
  '스포츠': {
    '축구': ['축구', 'K리그', '프리미어리그', '손흥민', '챔피언스리그'],
    '야구': ['야구', 'KBO', 'MLB', '홈런', '투수'],
    '골프': ['골프', 'LPGA', 'PGA'],
    '농구_배구': ['농구', '배구', 'KBL', 'V리그', 'NBA'],
    '올림픽_아시안게임': ['올림픽', '아시안게임', '패럴림픽'],
    '월드컵': ['월드컵'],
    '스포츠일반': [],
  },
};
// 같은 쪽에서 여러 검색어로 찾은 기사는 아래 정의 순서로 1순위 분류를 정한다.
const KEYWORD_GROUPS = {
  정치: [
    ['국회', '국회_정당'], ['대통령', '청와대'], ['선거'], ['북한'], ['외교'],
    ['여당', '국회_정당'], ['야당', '국회_정당'], ['법안', '국회_정당'],
    ['대통령실', '청와대'], ['공천', '선거'], ['국방', '외교'], ['정상회담', '외교'], ['정부', '행정_자치'],
  ],
  경제: [
    ['증시', '증권_증시'], ['금리', '금융_재테크'], ['부동산'], ['반도체'], ['환율', '외환'],
    ['유통'], ['자동차'], ['관세', '무역'], ['코스피', '증권_증시'], ['주가', '증권_증시'],
    ['대출', '금융_재테크'], ['아파트', '부동산'], ['물가', '경제일반'], ['수출', '무역'], ['배터리', '산업_기업'],
  ],
  사회: [
    ['경찰', '사건_사고'], ['검찰', '사건_사고'], ['법원', '사건_사고'], ['화재', '사건_사고'],
    ['교육', '교육_시험'], ['노동', '노동_복지'], ['의료', '의료_건강'], ['사건', '사건_사고'],
    ['사고', '사건_사고'], ['날씨'], ['기후', '환경'], ['복지', '노동_복지'], ['교통', '사회일반'], ['병원', '의료_건강'],
  ],
  국제: [
    ['미국', '미국_북미'], ['중국'], ['일본'], ['유럽', '유럽_EU'], ['중동', '중동_아프리카'],
    ['러시아'], ['우크라이나', '유럽_EU'], ['이스라엘', '중동_아프리카'],
    ['대만', '중국'], ['인도', '아시아'], ['영국', '유럽_EU'], ['아세안', '아시아'],
  ],
  방송_연예: [
    ['연예'], ['드라마'], ['예능'], ['OTT'], ['아이돌'],
    ['배우'], ['가수'], ['방송'], ['케이팝'], ['연예인'],
  ],
  문화: [
    ['공연', '전시_공연'], ['영화'], ['출판'], ['전시', '전시_공연'], ['뮤지컬', '전시_공연'],
    ['문화재', '학술_문화재'], ['여행', '요리_여행'], ['축제', '요리_여행'],
    ['미술', '미술_건축'], ['클래식', '음악'], ['패션', '생활'],
  ],
  IT_과학: [
    ['AI', 'IT_과학일반'], ['과학'], ['보안'], ['게임', '콘텐츠'],
    ['인공지능', 'IT_과학일반'], ['스마트폰', '모바일'], ['개인정보', '보안'],
    ['해킹', '보안'], ['클라우드', 'IT_과학일반'], ['우주', '과학'],
    ['로봇', '과학'], ['바이오', '과학'], ['SNS', '인터넷_SNS'],
  ],
  스포츠: [
    ['축구'], ['야구'], ['골프'], ['농구', '농구_배구'], ['배구', '농구_배구'],
    ['올림픽', '올림픽_아시안게임'], ['월드컵'], ['K리그', '축구'], ['KBO', '야구'],
    ['테니스', '스포츠일반'], ['배드민턴', '스포츠일반'], ['아시안게임', '올림픽_아시안게임'],
  ],
};
const KEYWORDS = Object.fromEntries(Object.entries(KEYWORD_GROUPS).flatMap(([group, entries]) =>
  entries.map(([keyword, minor = keyword]) => [keyword,
    group === '방송_연예' ? ['문화', group] : [group, minor]])));

const SEARCH_API_CALL_CAP = 1000;

// 후보 전체가 분류/메뉴 단어와 일치할 때만 제외한다. 기자 이름의 부분 문자열에는 적용하지 않는다.
// 키워드 집계용 STOPWORDS와 분리해 중국·시민 같은 주제 명사의 언급 횟수는 보존한다.
const REPORTER_TOPIC_WORDS = new Set([
  ...Object.keys(KEYWORDS),
  ...Object.entries(CATEGORIES).flatMap(([major, minors]) =>
    [major, ...Object.entries(minors).flatMap(([minor, words]) => [minor, ...words])]),
  '글로벌', '국제', '부고', '시민', '스포츠', '연예', '오피니언', '사설', '칼럼', '인물',
  '포토', '영상', '속보', '단독', '인사', '동정', '기획', '특집', '지역', '전국',
  // 분류 이름이 바뀌어도 이전 섹션명과 지역명은 기자명에서 계속 제외한다.
  '세계', '중기', '생활경제', '사건사고', '건강정보', '시승기', '뉴미디어', '일반', '리뷰',
  '서울시', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '경기', '강원',
  '충북', '충남', '전북', '전남', '경북', '경남', '제주',
].flatMap(word => [word, ...word.split(/[\s·/_]+/)]));
