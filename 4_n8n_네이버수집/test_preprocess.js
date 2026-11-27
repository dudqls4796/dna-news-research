// 합성 HTML만 메모리에서 검사. 본문/CSV/응답 로그·파일 쓰기 없음.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const { EventEmitter } = require('events');
const source = f => fs.readFileSync(`${__dirname}/${f}`, 'utf8');
const common = source('news_common.js');
let clock = Date.parse('2026-11-16T04:05:00Z'), execution = 'run-1';
class Clock extends Date {
  constructor(...args) { super(...(args.length ? args : [clock])); }
  static now() { return clock; }
}
const calls = [], sleeps = [], routes = new Map(), store = {};
const transport = { get(url, options, callback) {
  const req = new EventEmitter(); req.destroy = () => {};
  assert.ok(options.headers['User-Agent'].startsWith('DICEArticleCollector/'));
  assert.strictEqual(options.headers['Accept-Encoding'], 'identity');
  calls.push({ url: url.href, at: clock });
  process.nextTick(() => {
    const route = routes.get(url.href);
    if (!route || route.error) return req.emit('error', new Error('합성 요청 실패'));
    clock += route.elapsed || 0;
    const res = new EventEmitter();
    Object.assign(res, { statusCode: route.status || 200, headers: route.headers || { 'content-type': 'text/html; charset=utf-8' }, resume() {}, destroy() {} });
    callback(res);
    if (res.statusCode !== 200) return;
    const bytes = route.bytes || Buffer.from(route.html || '');
    res.emit('data', bytes.subarray(0, 17)); res.emit('data', bytes.subarray(17)); res.emit('end');
  });
  return req;
} };
function context(extra = {}) {
  return { Buffer, TextDecoder, Date: Clock,
    console: { log() { throw new Error('노드 로그 금지'); } },
    require(name) { if (['http', 'https'].includes(name)) return transport; assert.strictEqual(name, 'url'); return require('url'); },
    setTimeout(fn, ms) { if (String(fn).includes('finish(')) return setTimeout(fn, ms); sleeps.push(ms); clock += ms; process.nextTick(fn); return null; }, clearTimeout,
    $getWorkflowStaticData: () => store, $execution: { id: execution }, ...extra };
}
function runCode(code, items = [], extra = {}) {
  return vm.runInNewContext(`(async () => {${code}\n})()`, context({
    $input: { all: () => items, first: () => items[0] }, ...extra }));
}
const run = (file, items, extra) => runCode(common + '\n' + source(file), items, extra);
const plain = value => JSON.parse(JSON.stringify(value));
const robots = (origin, text = 'User-agent: *\nDisallow: /blocked') => routes.set(origin + '/robots.txt', { html: text });
const sentence = '가상연구소의 합성센서는 시험정원에서 푸른신호를 반복하여 측정했다.';
const body = `<p>반도체 반도체 ${sentence}</p><p>${sentence} ${sentence}</p>`;
const page = (contents = body, author = '<meta name="author" content="홍길동 기자 (hong@example.test)">') =>
  `<meta charset="utf-8">${author}<script>저장금지스크립트</script><nav>저장금지메뉴</nav><div id="articleBody"><div>${contents}</div><p>홍길동 기자 hong@example.test https://example.test/private</p><p>Copyright 합성언론 무단전재 및 재배포 금지</p></div><footer>저장금지꼬리</footer>`;
const item = (title, path, minutes = 5) => ({ title, originallink: `https://www.donga.com/${path}`, link: `https://n.news.naver.com/${path}`,
  description: 'API설명조각저장금지', pubDate: new Date(Date.parse('2026-11-16T04:00:00Z') - minutes * 60e3).toUTCString() });
async function collectPages(runNode, window, responseFor) {
  const history = [];
  const requestsByRound = [];
  let cursor = { json: { window } };
  do {
    const round = history.length;
    assert.ok(round < 10, '최대 10쪽');
    const requests = await runNode('검색 요청 만들기', [cursor]);
    assert.ok(requests.length > 0);
    requestsByRound.push(Array.from(requests, ({ json }) => json.keyword));
    const responses = requests.map(({ json: { keyword, page, start, display } }, index) => {
      assert.strictEqual(page, round + 1);
      assert.strictEqual(start, round * 100 + 1);
      assert.strictEqual(display, 100);
      return { json: responseFor(keyword, page), pairedItem: { item: index } };
    }).reverse();
    const [result] = await runNode('검색 결과 정리', responses, { $runIndex: round, $: name => {
      if (name === '검색 요청 만들기') return { itemMatching: i => requests[responses[i].pairedItem.item] };
      assert.strictEqual(name, '검색 결과 정리');
      return { all: (_, index) => [history[index]] };
    } });
    history.push(result);
    cursor = result;
  } while (cursor.json.hasNext);
  return { result: cursor.json, requestsByRound };
}

const makePage = (count, minutes) => Array.from({ length: count }, (_, i) => item('쪽 검사', `page-${i}`, minutes));
async function checkPagination(runNode, window, keywords) {
  const { result, requestsByRound } = await collectPages(runNode, window, (keyword, page) => {
    switch (keyword) {
      case '국회': return { items: makePage(100, 59) };
      case '대통령': return { items: makePage(100, 61) };
      case '북한': return { items: page === 1 ? makePage(100, 60) : [] };
      case '선거': return page === 1 ? { items: makePage(100, 5) } : { error: '가상 HTTP 오류' };
      case '외교': {
        const items = page === 1 ? makePage(100, 5) : makePage(3, 5);
        if (page === 1) items.at(-1).pubDate = '잘못된 날짜';
        return { items };
      }
      default: return { items: [] };
    }
  });
  assert.deepStrictEqual(requestsByRound[0], keywords);
  assert.deepStrictEqual(requestsByRound.slice(1).map(requests => requests.length), [4, 1, 1, 1, 1, 1, 1, 1, 1]);
  const { apiCalls, capReached, skippedKeywords, byCategory, pagesByKeyword, possiblyTruncated, failedPages } = plain(result.stats);
  assert.strictEqual(apiCalls, keywords.length + 12);
  assert.strictEqual(capReached, false);
  assert.deepStrictEqual(skippedKeywords, []);
  assert.strictEqual(Object.keys(pagesByKeyword).length, keywords.length);
  assert.deepStrictEqual([pagesByKeyword.국회, pagesByKeyword.대통령, pagesByKeyword.북한, pagesByKeyword.선거, pagesByKeyword.외교, pagesByKeyword.축구], [10, 1, 2, 2, 2, 1]);
  assert.deepStrictEqual(possiblyTruncated, ['국회']);
  assert.deepStrictEqual(failedPages, [{ keyword: '선거', page: 2 }]);
  assert.strictEqual(byCategory.정치.possiblyTruncated, 1);
  assert.strictEqual(byCategory.방송_연예.possiblyTruncated, 0);
  assert.strictEqual(Object.values(byCategory).reduce((sum, counts) => sum + counts.apiCalls, 0), apiCalls);
  assert.ok(!JSON.stringify(result).includes('API설명조각저장금지'));
  return result.stats;
}

async function checkCallCap(runNode, runWithSmallerCap, window, groups, keywords) {
  const fullPage = { items: makePage(100, 5) };
  const { result, requestsByRound } = await collectPages(runNode, window, () => fullPage);
  const stats = plain(result.stats);
  assert.strictEqual(requestsByRound.length, 10);
  for (const requests of requestsByRound) assert.deepStrictEqual(requests, keywords);
  assert.strictEqual(requestsByRound.flat().length, keywords.length * 10);
  assert.ok(requestsByRound.flat().length <= 1000);
  assert.strictEqual(stats.apiCalls, requestsByRound.flat().length);
  assert.strictEqual(stats.capReached, stats.apiCalls === 1000);
  assert.deepStrictEqual(stats.skippedKeywords, []);
  assert.deepStrictEqual(stats.possiblyTruncated, keywords);
  for (const [category, entries] of Object.entries(groups)) {
    assert.deepStrictEqual(stats.byCategory[category], { apiCalls: entries.length * 10, possiblyTruncated: entries.length });
  }

  const { result: limited, requestsByRound: limitedRounds } = await collectPages(runWithSmallerCap, window, (keyword, page) =>
    keyword === keywords[0] && page === 2 ? { items: [] } : fullPage);
  assert.deepStrictEqual(limitedRounds, [keywords, keywords.slice(0, 3)]);
  assert.strictEqual(limited.stats.apiCalls, keywords.length + 3);
  assert.strictEqual(limited.stats.capReached, true);
  const skipped = keywords.slice(1);
  assert.deepStrictEqual(plain(limited.stats.skippedKeywords), skipped);
  assert.deepStrictEqual(plain(limited.stats.possiblyTruncated), skipped);
  assert.strictEqual(limited.stats.byCategory.정치.apiCalls, groups.정치.length + 3);
  assert.strictEqual(limited.stats.byCategory.정치.possiblyTruncated, groups.정치.length - 1);
  assert.strictEqual(limited.stats.byCategory.방송_연예.apiCalls, groups.방송_연예.length);
  assert.strictEqual(limited.stats.byCategory.방송_연예.possiblyTruncated, groups.방송_연예.length);

  const pages = keywords.slice(0, 3).map(keyword => ({ keyword, page: 9, hasNext: true }));
  const lastRequest = await runNode('검색 요청 만들기', [{ json: { window, pages, apiCalls: 999 } }]);
  assert.strictEqual(lastRequest.length, 1);
  assert.strictEqual(lastRequest[0].json.apiCalls, 1000);
  const exhausted = await runNode('검색 요청 만들기', [{ json: { window, pages, apiCalls: 1000 } }]);
  assert.strictEqual(exhausted.length, 0);
}

async function main() {
  const workflow = JSON.parse(source('naver_news_csv_workflow.json'));
  const nodeOf = name => workflow.nodes.find(n => n.name === name);
  const runNode = (name, items, extra) => runCode(nodeOf(name).parameters.jsCode, items, extra);
  for (const [name, file] of [['검색 요청 만들기', 'build_requests.js'], ['검색 결과 정리', 'search_results.js'],
    ['원문 대상 만들기', 'prepare_articles.js'], ['전처리·CSV 만들기', 'preprocess.js']])
    assert.strictEqual(nodeOf(name).parameters.jsCode, common + '\n' + source(file));
  for (const node of workflow.nodes.filter(n => n.type === 'n8n-nodes-base.code')) {
    new vm.Script(`(async () => {${node.parameters.jsCode}\n})`);
    for (const [, module] of node.parameters.jsCode.matchAll(/require\s*\(\s*['"]([^'"]+)['"]\s*\)/g))
      assert.ok(['http', 'https', 'url'].includes(module));
  }
  assert.strictEqual(nodeOf('매시간 정각').parameters.rule.interval[0].expression, '0 * * * *');
  assert.strictEqual(workflow.settings.executionTimeout, 3300);
  assert.strictEqual(nodeOf('네이버 뉴스 검색').retryOnFail, false);
  assert.strictEqual(nodeOf('네이버 뉴스 검색').parameters.options.redirect.redirect.followRedirects, false);
  assert.strictEqual(nodeOf('DB 저장').parameters.query, source('db_insert.sql'));
  assert.deepStrictEqual(workflow.connections['다음 검색 쪽 여부'].main.map(branch => branch[0].node), ['검색 요청 만들기', '원문 대상 만들기']);
  assert.strictEqual(workflow.connections['DB 저장'].main[0][0].node, '임베딩 요청');
  assert.deepStrictEqual(['CSV 저장', '임베딩 요청'].map(name => workflow.connections[name].main[0][0].index), [0, 1]);
  const requestNode = workflow.nodes.find(n => n.id === 'n2');
  const initial = () => runCode(requestNode.parameters.jsCode, [{ json: {} }]);
  const window = (await initial())[0].json.window;
  assert.strictEqual(window.window_id, '2026111613');
  const originalClock = clock;
  clock = Date.parse('2026-11-16T00:00:01+09:00');
  const midnight = (await initial())[0].json.window;
  clock = originalClock;
  assert.strictEqual(midnight.window_id, '2026111600');
  assert.strictEqual(new Date(midnight.since).toISOString(), '2026-11-15T14:00:00.000Z');
  const publishers = vm.runInNewContext(`(() => {${common}\nreturn { publisherOf, KEYWORDS, KEYWORD_GROUPS, CATEGORIES, SEARCH_API_CALL_CAP }; })()`, context());
  const groups = plain(publishers.KEYWORD_GROUPS);
  const keywords = Object.values(groups).flatMap(entries => entries.map(([keyword]) => keyword));
  assert.deepStrictEqual(Object.keys(groups).sort(), ['정치', '경제', '사회', '문화', '방송_연예', '국제', 'IT_과학', '스포츠'].sort());
  assert.ok(Object.values(groups).every(entries => entries.length > 0));
  assert.ok(keywords.length <= 100);
  assert.strictEqual(new Set(keywords).size, keywords.length);
  assert.deepStrictEqual(Object.keys(publishers.KEYWORDS), keywords);
  assert.strictEqual(publishers.SEARCH_API_CALL_CAP, 1000);
  assert.ok(24 * publishers.SEARCH_API_CALL_CAP < 25000);
  const stats = await checkPagination(runNode, window, keywords);
  const runWithSmallerCap = (name, items, extra) => runCode(nodeOf(name).parameters.jsCode.replace(
    'const SEARCH_API_CALL_CAP = 1000;', `const SEARCH_API_CALL_CAP = ${keywords.length + 3};`), items, extra);
  await checkCallCap(runNode, runWithSmallerCap, window, groups, keywords);
  const summaryNode = workflow.nodes.find(n => n.id === 'n16');
  const summary = await runCode(summaryNode.parameters.jsCode, [], {
    $: () => ({ first: () => ({ json: { window_id: window.window_id, stats } }) }),
  });
  assert.deepStrictEqual(plain(summary[0].json.stats), plain(stats));
  const prepare = pages => run('prepare_articles.js', [{ json: { window, pages } }]);
  assert.strictEqual(publishers.publisherOf('https://sports.donga.com/article'), '스포츠동아');
  assert.strictEqual(publishers.publisherOf('https://www.sports.donga.com/article'), '스포츠동아');
  assert.strictEqual(publishers.publisherOf('https://www.donga.com/article'), '동아일보');
  assert.strictEqual(publishers.publisherOf('https://www.chosun.com/article'), '조선일보');
  assert.strictEqual(publishers.publisherOf('https://www.ajunews.com/article/1'), '아주경제');
  assert.strictEqual(publishers.publisherOf('https://news.ajunews.com/article/1'), '아주경제');
  assert.strictEqual(publishers.publisherOf('https://n.news.naver.com/mnews/article/009/12345'), '매일경제');
  assert.strictEqual(publishers.publisherOf('https://n.news.naver.com/mnews/article/999/12345'), 'news.naver.com');
  assert.strictEqual(publishers.publisherOf('https://evilnaver.com/mnews/article/009/123'), 'evilnaver.com');
  assert.strictEqual(publishers.publisherOf('https://daily.hankooki.com/a'), '데일리한국');
  const duplicateSource = { ...item('네이버 링크 중복', 'different-url'), link: item('', 'utf8').link };
  const responses = [
    { keyword: '증시', items: [item('반도체, 코스피 상승', 'utf8', 60), item('오래된 기사', 'old', 61), item('다음 구간 기사', 'next', 0)] },
    { keyword: '북한', items: [item('<b>북한</b> 동해상 탄도 미사일 발사 합참', 'failed', 2), item('북한 동해상 탄도 미사일 발사 합참 발표', 'blocked', 3), item('중복', 'utf8'), duplicateSource] },
  ];
  clock += 60 * 60e3;
  const articles = await prepare(responses);
  clock -= 60 * 60e3;
  assert.strictEqual(articles.length, 3, '구간 밖·중복 제거');
  assert.strictEqual(articles.find(a => a.json.url.endsWith('/utf8')).json.keyword, '증시', '중복 기사에 먼저 받은 검색어 사용');
  assert.ok(!JSON.stringify(articles).includes('API설명조각저장금지'));
  assert.ok(articles.every(a => /^2026111613\d{5}$/.test(a.json.id)));
  const dailyTitle = '한글 기사';
  const article = item(dailyTitle, 'first-title');
  const normalizedCopy = item('  <b>한글</b>\u00a0  기사 '.normalize('NFD'), 'second-title');
  const photo = { ...item(dailyTitle, 'photo'), originallink: 'https://www.yna.co.kr/view/PYH202611240001' };
  const report = { ...item(dailyTitle, 'report'), originallink: 'https://www.yna.co.kr/view/AKR202611240001' };
  const deduped = await prepare([{ keyword: '북한', items: [article, normalizedCopy, photo, report] }]);
  assert.deepStrictEqual(Array.from(deduped, ({ json }) => json.url), [article.originallink, report.originallink]);
  robots('https://www.donga.com'); routes.set('https://www.donga.com/utf8', { html: page() });
  const enriched = [];
  for (const article of articles) enriched.push(...await run('enrich_articles.js', [article]));
  const success = enriched.find(a => a.json.url.endsWith('/utf8')).json;
  assert.strictEqual(success.byline, '홍길동'); assert.strictEqual(success.kw['반도체'], 3, '제목+본문 횟수');
  assert.strictEqual(success.analysis_status, 'body_counted');
  for (const a of enriched.filter(a => !a.json.url.endsWith('/utf8'))) {
    assert.strictEqual(a.json.byline, ''); assert.strictEqual(a.json.analysis_status, 'title_fallback'); assert.strictEqual(a.json.kw['북한'], 1);
  }
  assert.ok(!calls.some(c => c.url.endsWith('/blocked')), 'robots 불허 요청 0회');
  assert.strictEqual(calls.filter(c => c.url.endsWith('/robots.txt')).length, 1);
  assert.ok(calls.slice(1).every((c, i) => c.at - calls[i].at >= 500), 'robots 포함 2 req/s');
  const csvOut = await run('preprocess.js', enriched.reverse());
  const csv = Object.fromEntries(csvOut.map(o => [o.json.table, Buffer.from(o.binary.data.data, 'base64').toString('utf8')]));
  assert.deepStrictEqual(Object.keys(csv), ['articles', 'article_sources', 'article_keywords', 'article_categories', 'article_groups']);
  assert.ok(/^\d{10}_articles\.csv$/.test(csvOut[0].binary.data.fileName));
  assert.strictEqual(csv.articles.trim().split('\r\n').length, 4);
  assert.strictEqual(csv.articles.split('\r\n')[0], '\ufeffid,source_id,published_date,title,loaded_at,url');
  assert.strictEqual(csv.article_categories.split('\r\n')[0], '\ufeffarticle_id,level,major,minor');
  assert.ok(csv.articles.includes(',"반도체, 코스피 상승",'), '쉼표가 있는 제목은 articles에 CSV 이스케이프');
  assert.ok(!csv.article_categories.includes('반도체, 코스피 상승'), '분류 CSV에 제목 없음');
  assert.ok(/,\d{4}-\d{2}-\d{2},.*\+09:00,/.test(csv.articles));
  assert.ok(csv.article_keywords.includes(`${success.id},반도체,3`));
  assert.ok(csv.article_sources.includes(`${success.id},동아일보,홍길동`));
  assert.ok(csv.article_categories.includes(`${success.id},1,경제,증권_증시`));
  const sportsCsv = await run('preprocess.js', [{ json: { ...success, url: 'https://sports.donga.com/article', keyword: '연예', title: '합성 소식' } }]);
  const sportsTables = Object.fromEntries(sportsCsv.map(o => [o.json.table, Buffer.from(o.binary.data.data, 'base64').toString('utf8')]));
  assert.ok(sportsTables.article_sources.includes(`${success.id},스포츠동아,홍길동`));
  assert.ok(sportsTables.article_categories.includes('문화,방송_연예'));
  const classify = vm.runInNewContext(`(() => {${common}\n${source('preprocess.js').split('const csvCell')[0]}\nreturn categoriesOf; })()`, context());
  const taxonomy = {
    정치: '국회_정당 청와대 선거 외교 행정_자치 북한 정치일반',
    경제: '증권_증시 유통 금융_재테크 부동산 자동차 경제일반 산업_기업 반도체 취업_창업 서비스_쇼핑 자원 무역 국제경제 외환',
    사회: '사건_사고 교육_시험 사회일반 노동_복지 의료_건강 여성 날씨 미디어 장애인 환경',
    국제: '미국_북미 중동_아프리카 유럽_EU 중국 일본 아시아 러시아 중남미 국제일반',
    문화: '방송_연예 전시_공연 출판 영화 요리_여행 미술_건축 종교 문화일반 생활 학술_문화재 음악',
    IT_과학: '과학 모바일 보안 콘텐츠 IT_과학일반 인터넷_SNS',
    스포츠: '축구 야구 골프 농구_배구 올림픽_아시안게임 월드컵 스포츠일반',
  };
  assert.deepStrictEqual(Object.keys(publishers.CATEGORIES).sort(), Object.keys(taxonomy).sort());
  for (const [major, minors] of Object.entries(taxonomy))
    assert.deepStrictEqual(Object.keys(publishers.CATEGORIES[major]).sort(), minors.split(' ').sort());
  const mapping = {
    정치: '국회:국회_정당 대통령:청와대 선거 북한 외교',
    경제: '증시:증권_증시 금리:금융_재테크 부동산 반도체 환율:외환 유통 자동차 관세:무역',
    사회: '경찰:사건_사고 검찰:사건_사고 법원:사건_사고 화재:사건_사고 교육:교육_시험 노동:노동_복지 의료:의료_건강',
    국제: '미국:미국_북미 중국 일본 유럽:유럽_EU 중동:중동_아프리카',
    문화: '연예:방송_연예 드라마:방송_연예 공연:전시_공연 영화 출판',
    IT_과학: 'AI:IT_과학일반 과학 보안 게임:콘텐츠',
    스포츠: '축구 야구 골프 농구:농구_배구 배구:농구_배구 올림픽:올림픽_아시안게임',
  };
  const expectedKeywords = {};
  for (const [major, pairs] of Object.entries(mapping)) {
    for (const pair of pairs.split(' ')) {
      const [keyword, minor = keyword] = pair.split(':');
      expectedKeywords[keyword] = [major, minor];
      const result = classify('경찰 검찰 구속 수사 코스피 반도체', keyword);
      assert.deepStrictEqual(plain(result[0]), { major, minor });
      assert.strictEqual(new Set(result.map(c => `${c.major}/${c.minor}`)).size, result.length);
      assert.ok(result.length <= 3);
    }
  }
  assert.strictEqual(Object.keys(expectedKeywords).length, 40);
  for (const [keyword, categories] of Object.entries(expectedKeywords))
    assert.deepStrictEqual(plain(publishers.KEYWORDS[keyword]), categories, keyword);
  for (const [group, entries] of Object.entries(groups)) {
    for (const [keyword] of entries) {
      const [major, minor] = publishers.KEYWORDS[keyword];
      assert.strictEqual(major, group === '방송_연예' ? '문화' : group);
      assert.ok(Object.hasOwn(publishers.CATEGORIES[major], minor), keyword);
      if (group === '방송_연예') assert.strictEqual(minor, group, keyword);
      else if (group === '문화') assert.notStrictEqual(minor, '방송_연예', keyword);
    }
  }
  for (const [title, minor] of [
    ['손흥민 K리그 축구', '축구'], ['KBO 야구 홈런 투수', '야구'], ['LPGA 골프 대회', '골프'],
    ['KBL 농구 경기', '농구_배구'], ['V리그 배구 경기', '농구_배구'],
    ['올림픽 패럴림픽', '올림픽_아시안게임'], ['아시안게임 개막', '올림픽_아시안게임'], ['월드컵', '월드컵'],
  ]) assert.ok(classify(title, '').some(c => c.major === '스포츠' && c.minor === minor), title);
  assert.ok(!classify('아시안게임 개막', '올림픽').some(c => c.major === 'IT_과학'));
  assert.ok(classify('아시안게임 게임 출시', '올림픽').some(c => c.minor === '콘텐츠'));
  for (const [title, keyword, major, minor] of [
    ['국회 법안 논의', '', '정치', '국회_정당'], ['반도체 코스피 상승', '반도체', '경제', '반도체'],
    ['경찰 구속 수사', '', '사회', '사건_사고'], ['영화 드라마 공개', '연예', '문화', '방송_연예'],
    ['미국 백악관 발표', '', '국제', '미국_북미'], ['e스포츠 게임 대회', '', 'IT_과학', '콘텐츠'],
    ['합성 소식', '연예', '문화', '방송_연예'],
  ]) {
    const result = classify(title, keyword);
    assert.strictEqual(result[0].major, major, title);
    assert.strictEqual(result[0].minor, minor, title);
    assert.ok(!result.some(c => c.major === '스포츠'), `${title}: 스포츠 오분류`);
  }
  assert.strictEqual(csv.article_groups.trim().split('\r\n').length, 3, '제목 기반 그룹');
  assert.strictEqual(csv.article_groups.split('\r\n').filter(r => r.endsWith(',true')).length, 1);
  for (const text of [JSON.stringify(enriched), JSON.stringify(store), ...Object.values(csv)])
    for (const forbidden of [sentence, 'API설명조각저장금지', '저장금지메뉴', '저장금지스크립트', '저장금지꼬리', 'hong@example.test', '무단전재', '합성언론'])
      assert.ok(!text.includes(forbidden), '본문 문장·노이즈 저장 금지');
  assert.ok(!Object.keys(success.kw).some(w => w.includes('홍길동')));
  assert.ok(!Object.keys(success).some(k => /body|html|content|description|bytes/i.test(k)));
  const kb = Buffer.from([0xb0, 0xa1, 0xb3, 0xaa]); // 합성 '가나' EUC-KR
  assert.strictEqual(new TextDecoder('euc-kr').decode(kb), '가나');
  const euc = Buffer.concat([Buffer.from('<meta charset="euc-kr"><meta name="author" content="&#54861;&#44600;&#46041;"><article><p>'),
    ...Array.from({ length: 40 }, () => Buffer.concat([kb, Buffer.from(' ')])), Buffer.from('</p></article>')]);
  const base = { ...articles[2].json, url: 'https://euc.test/a', title: '가나', id: '202611161300004' };
  robots('https://euc.test');
  for (const headers of [{ 'content-type': 'text/html' }, { 'content-type': 'text/html; charset=EUC-KR' }]) {
    routes.set(base.url, { bytes: euc, headers });
    const out = await run('enrich_articles.js', [{ json: base }]);
    assert.strictEqual(out[0].json.kw['가나'], 41, 'meta/HTTP charset EUC-KR'); assert.strictEqual(out[0].json.byline, '홍길동');
  }
  const helpers = vm.runInNewContext(`(() => {${common}\n${source('enrich_articles.js').split('const output = [];')[0]}
    return { parseRobots, robotsAllowed, extractPage, stripBody, namesOf, keywordsOf, decodeEntities, clean, requestOnce }; })()`, context());
  assert.deepStrictEqual(plain(helpers.namesOf('홍길동 기자, 김철수 기자 (kim@example.test)')), ['홍길동', '김철수']);
  assert.strictEqual(helpers.extractPage(page(body, '<meta property="dable:author" content="홍길동, 김철수">')).byline, '홍길동, 김철수');
  assert.strictEqual(helpers.extractPage(page(body, '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","author":[{"@type":"Person","name":"홍길동"},{"@type":"Person","name":"김철수"}]}]}</script>')).byline, '홍길동, 김철수');
  assert.strictEqual(helpers.extractPage(`<article>${body}<p>홍길동 기자 (hong@example.test)</p></article>`).byline, '홍길동');
  assert.strictEqual(helpers.namesOf({ '@type': 'Organization', name: '합성신문' }).length, 0);
  // v0.1.62: 기자명이 아닌 언론사·일반어를 걸러내고 다음 출처로 계속 탐색한다.
  for (const name of ['한국경제', '한국', '매일경제', '중앙일보', '중앙', '한겨레', '촉구하는', '대신', '관련', '관련된',
    '기자', '특파원', '논설위원', '편집국', '뉴스', '일보', '신문', '경제', '방송', '추진한', '김철수는', '하는', '한', '홍길동철수', '홍 길동', 'Synthetic Reporter'])
    assert.strictEqual(helpers.namesOf(name).length, 0, '비기자 후보 제외');
  assert.deepStrictEqual(plain(helpers.namesOf('홍길동, 한국경제, 김철수')), ['홍길동', '김철수']);
  assert.deepStrictEqual(plain(helpers.namesOf('남궁민수 특파원')), ['남궁민수']);
  assert.strictEqual(helpers.namesOf('별빛', '별빛소식').length, 0, '현재 언론사명의 일부도 제외');
  const reporterPage = (meta, signature = '', ld = '') => `${meta}${ld}<article>${body}<p>${signature}</p></article>`;
  const articleLd = author => `<script type="application/ld+json">${JSON.stringify({ '@type': 'NewsArticle', author })}</script>`;
  const publisherMeta = '<meta name="author" content="한국경제">';
  assert.strictEqual(helpers.extractPage(reporterPage(publisherMeta), '한국경제').byline, '', '언론사만 있으면 NULL');
  assert.strictEqual(helpers.extractPage(reporterPage(publisherMeta, '김철수 특파원'), '한국경제').byline, '김철수', '언론사 메타보다 실제 특파원 서명');
  assert.strictEqual(helpers.extractPage(reporterPage(publisherMeta, '김철수 기자', articleLd({ '@type': 'Person', name: '홍길동' })), '한국경제').byline, '홍길동', '무효 메타 다음 JSON-LD');
  assert.strictEqual(helpers.extractPage(reporterPage(publisherMeta, '김철수 기자', articleLd({ '@type': 'Organization', name: '한국경제' })), '한국경제').byline, '김철수', '기관 JSON-LD도 서명으로 대체');
  assert.strictEqual(helpers.extractPage(reporterPage('<meta name="author" content="촉구하는, 대신, 관련">', '촉구하는 기자 대신 기자 관련 기자'), '중앙일보').byline, '', '일반어 서명 오탐 방지');
  assert.strictEqual(helpers.extractPage(reporterPage('<meta name="author" content="촉구하는, 대신, 관련">', '홍길동 기자'), '중앙일보').byline, '홍길동');
  assert.strictEqual(helpers.extractPage(reporterPage('<meta property="og:site_name" content="별빛소식"><meta name="author" content="별빛소식">', '김철수 기자')).byline, '김철수', '미등록 언론사 og:site_name도 검증');
  assert.strictEqual(helpers.extractPage(reporterPage('', '가상연구소기자 홍길동 기자')).byline, '홍길동', '긴 일반어의 일부를 이름으로 추출하지 않음');

  // v0.1.63: 섹션/메뉴와 기자명이 혼합된 메타·JSON-LD·서명에서 유효 이름만 보존한다.
  for (const word of ['글로벌', '중국', '부고', '시민', '정치', '경제', '사회', '국제', '문화', '스포츠', '연예',
    '오피니언', '사설', '칼럼', '인물', '포토', '영상', '속보', '단독', '대통령실', '금융', '반도체', '과학', '생활', '세계', '부산'])
    assert.strictEqual(helpers.namesOf(word).length, 0, '분류 이름·분류 키워드·메뉴 단어를 기자명에서 제외');
  const mixedBylines = [
    { origin: 'https://www.newspim.com', dirty: '글로벌, 중국, 부고, 서영욱, 권지언', expected: '서영욱, 권지언' },
    { origin: 'https://www.goodmorningcc.com', dirty: '홍순구, 시민', expected: '홍순구' },
  ];
  for (const [caseIndex, fixture] of mixedBylines.entries()) {
    const candidates = fixture.dirty.split(', ');
    const pages = [
      reporterPage(`<meta name="author" content="${fixture.dirty}">`),
      reporterPage('', '', articleLd(candidates.map(name => ({ '@type': 'Person', name })))),
      reporterPage('', candidates.map(name => `${name} 기자`).join(' ')),
    ];
    robots(fixture.origin);
    for (const [format, html] of pages.entries()) {
      assert.strictEqual(helpers.extractPage(html).byline, fixture.expected, '혼합 출처에서 유효 이름만 유지');
      const url = `${fixture.origin}/synthetic/${format}`;
      routes.set(url, { html });
      const result = await run('enrich_articles.js', [{ json: { ...base, url, id: `20261116130000${caseIndex + 5}` } }]);
      assert.strictEqual(result[0].json.byline, fixture.expected);
      assert.strictEqual(result[0].json.analysis_status, 'body_counted');
      const csvs = await run('preprocess.js', result);
      const sources = csvs.find(out => out.json.table === 'article_sources');
      assert.ok(Buffer.from(sources.binary.data.data, 'base64').toString('utf8').includes(fixture.expected));
      for (const text of [JSON.stringify(result), JSON.stringify(store), ...csvs.map(out => Buffer.from(out.binary.data.data, 'base64').toString('utf8'))])
        assert.ok(!text.includes(sentence), '혼합 기자명 회귀에서도 본문 미저장');
    }
  }
  assert.deepStrictEqual(plain(helpers.namesOf('서영욱, 권지언, 홍순구')), ['서영욱', '권지언', '홍순구']);
  assert.deepStrictEqual(plain(helpers.namesOf('이시민, 김중국')), ['이시민', '김중국'], '분류 단어 부분 문자열은 실제 이름에서 유지');
  assert.deepStrictEqual(plain(helpers.keywordsOf('중국 시민 글로벌')), { 중국: 1, 시민: 1, 글로벌: 1 }, '기자명 제외 단어를 키워드 불용어로 확대하지 않음');

  // 제목·본문 모두 named/numeric entity를 복원하며 알 수 없는 이름을 키워드로 세지 않는다.
  const entities = { middot: '·', hellip: '…', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”', laquo: '«', raquo: '»',
    ndash: '–', mdash: '—', nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", bull: '•', times: '×', copy: '©', reg: '®', trade: '™' };
  for (const [name, decoded] of Object.entries(entities)) assert.strictEqual(helpers.decodeEntities(`&${name};`), decoded);
  assert.strictEqual(helpers.decodeEntities('&#54620;&#xAE00; &#x1F; &#x1F600; &#xD800; &#1114112;'), '한글   😀    ');
  assert.strictEqual(helpers.decodeEntities('&unknownentity; &constructor;'), '   ');
  const entityRun = Object.keys(entities).map(n => `&${n};`).join(' ') + ' &unknownentity; &constructor; &#x1F;';
  const entityKw = helpers.keywordsOf(`반도체${entityRun}반도체`);
  assert.deepStrictEqual(plain(entityKw), { 반도체: 2 });
  const entityPrepared = await prepare([{ keyword: '반도체', items: [item('반도체&middot;도봉 &unknownentity;', 'entities')] }]);
  assert.strictEqual(entityPrepared[0].json.title, '반도체·도봉');
  const headlineFixtures = [
    ['與초선 55명 “최민희 <b>정치</b>적 거취 진지하게 판단하라” 성명', '與초선 55명 “최민희 정치적 거취 진지하게 판단하라” 성명'],
    ['<B>정치</B> 갈등', '정치 갈등'],
    ['1∼<b>9월</b> 수출', '1∼9월 수출'],
    ['10월 <b>1</b>~10일 수출', '10월 1~10일 수출'],
    ['1&#x223c;<b>9월</b> 수입', '1∼9월 수입'],
    ['정치  적대감', '정치 적대감'],
  ];
  const headlines = await prepare([{ keyword: '국회', items: headlineFixtures.map(([raw], i) => item(raw, `headline-${i}`)) }]);
  assert.deepStrictEqual(plain(headlines.map(out => out.json.title)), headlineFixtures.map(([, expected]) => expected));
  const functionWords = '있는 이들 함께 관심 위해 따라 이날 지난해 올해 통해 대한 했다 밝혔다 말했다 있다 없다 것으로 등을 이후 가운데 오전 오후 현재 경우 관련 하는 되는 한 가는 오는 이다 된다 대신 같은';
  assert.deepStrictEqual(plain(helpers.keywordsOf(functionWords)), {}, '일반 기능어와 조사 제거 잔여 토큰도 제외');
  const retained = helpers.keywordsOf('북한 남한 권한 제한 기한 서한 반도체 반도체 도봉 인공지능');
  assert.deepStrictEqual(plain(retained), { 반도체: 2, 북한: 1, 남한: 1, 권한: 1, 제한: 1, 기한: 1, 서한: 1, 도봉: 1, 인공지능: 1 }, '명사·주제어 및 횟수 보존');

  const regressionBody = `<p>${sentence} ${sentence}</p><p>도봉&middot;도봉 도봉&#183;도봉 ${functionWords} &unknownentity;</p><p>&copy; 합성저작권안내 재배포 금지</p>`;
  routes.set('https://www.donga.com/entities', { html: reporterPage(publisherMeta, '김철수 특파원', '').replace('</article>', regressionBody + '</article>') });
  const regressionOut = await run('enrich_articles.js', entityPrepared);
  assert.strictEqual(regressionOut[0].json.byline, '김철수');
  assert.strictEqual(regressionOut[0].json.kw['도봉'], 5, '제목 1회 + 엔티티로 구분한 본문 4회');
  for (const word of ['middot', 'unknownentity', '밝혔', '말했', '관심', '대신', '합성저작권안내']) assert.ok(!Object.hasOwn(regressionOut[0].json.kw, word));
  const regressionCsv = await run('preprocess.js', regressionOut);
  for (const out of regressionCsv) assert.ok(!Buffer.from(out.binary.data.data, 'base64').toString('utf8').includes(sentence), '회귀 fixture 본문 문장 CSV 미저장');
  assert.ok(!JSON.stringify(regressionOut).includes(sentence) && !JSON.stringify(store).includes(sentence), '회귀 fixture 노드 출력/staticData 미저장');
  const generic = helpers.extractPage(`<nav><p>저장금지노이즈</p></nav><div><p>짧은 메뉴</p></div><main><div>${body}</div></main>`);
  assert.ok(generic.text.includes(sentence) && !generic.text.includes('짧은 메뉴'));
  for (const attr of ['class="news_cnt_detail_wrap"', 'id="articletxt"', 'id="article_txt"', 'itemprop="articleBody"'])
    assert.ok(helpers.extractPage(`<div ${attr}>${body}</div>`).text.includes(sentence));
  const policy = helpers.parseRobots('User-agent: *\nDisallow: /\nUser-agent: DICEArticleCollector\nDisallow: /private*\nAllow: /private/open$\nCrawl-delay: 2\nRequest-rate: 1/3');
  assert.strictEqual(policy.gap, 3000);
  assert.ok(!helpers.robotsAllowed('https://x.test/private/a', policy));
  assert.ok(helpers.robotsAllowed('https://x.test/private/open', policy));
  assert.ok(!helpers.robotsAllowed('https://x.test/private/open?x=1', policy));
  assert.ok(helpers.robotsAllowed('https://x.test/public', policy));
  const many = helpers.keywordsOf('반복 반복 ' + Array.from({ length: 45 }, (_, i) => `항목${i}`).join(' '));
  assert.strictEqual(Object.keys(many).length, 30); assert.strictEqual(many['반복'], 2);
  assert.deepStrictEqual(Object.keys(many).slice(0, 3), ['반복', '항목0', '항목1']);
  const beforeCap = calls.length;
  const capped = await run('enrich_articles.js', [{ json: { ...base, open_page: false } }]);
  assert.strictEqual(calls.length, beforeCap); assert.strictEqual(capped[0].json.byline, ''); assert.strictEqual(capped[0].json.analysis_status, 'page_cap');
  const big = await prepare([{ keyword: '증시', items: Array.from({ length: 1002 }, (_, n) => item(`상한 검사 ${n}`, `cap/${n}`)) }]);
  assert.strictEqual(big.filter(a => a.json.open_page).length, 1000);
  const empty = await prepare([{ keyword: '증시', items: [], failed: true }]);
  const emptyCsv = await run('preprocess.js', await run('enrich_articles.js', empty));
  assert.strictEqual(emptyCsv.length, 5); assert.ok(emptyCsv.every(o => o.json.rows === 0));
  const uncategorized = { ...base, title: '분류없는제목', keyword: '', kw: {} };
  const uncategorizedCsv = await run('preprocess.js', [{ json: uncategorized }]);
  assert.strictEqual(uncategorizedCsv.find(o => o.json.table === 'article_categories').json.rows, 0);
  assert.ok(Buffer.from(uncategorizedCsv[0].binary.data.data, 'base64').toString('utf8').includes(',분류없는제목,'),
    '분류가 없는 기사도 제목 보존');
  robots('https://redirect.test'); robots('https://deny.test', 'User-agent: *\nDisallow: /');
  routes.set('https://redirect.test/a', { status: 302, headers: { location: 'https://deny.test/private' } });
  for (const url of ['https://redirect.test/a', 'https://missing-robots.test/a']) {
    const result = await run('enrich_articles.js', [{ json: { ...base, url } }]); assert.strictEqual(result[0].json.analysis_status, 'title_fallback');
  }
  assert.ok(!calls.some(c => ['https://deny.test/private', 'https://missing-robots.test/a'].includes(c.url)));
  robots('https://slow.test', 'User-agent: *\nCrawl-delay: 2'); routes.set('https://slow.test/a', { html: page() });
  await run('enrich_articles.js', [{ json: { ...base, url: 'https://slow.test/a' } }]);
  const slow = calls.filter(c => c.url.startsWith('https://slow.test/')); assert.ok(slow[1].at - slow[0].at >= 2000);
  robots('https://ten-delay.test', 'User-agent: *\nCrawl-delay: 10');
  routes.set('https://ten-delay.test/a', { html: page() });
  const ten = await run('enrich_articles.js', [{ json: { ...base, url: 'https://ten-delay.test/a' } }]);
  const tenCalls = calls.filter(c => c.url.startsWith('https://ten-delay.test/'));
  assert.strictEqual(ten[0].json.analysis_status, 'body_counted');
  assert.strictEqual(tenCalls[1].at - tenCalls[0].at, 10000, '10초 crawl-delay 대기');
  clock += 1000; // 다른 도메인의 기본 간격을 제외해 해당 기사의 sleep 여부를 확인한다.
  robots('https://long-delay.test', 'User-agent: *\nCrawl-delay: 60');
  const beforeSleeps = sleeps.length, started = process.hrtime.bigint();
  const delayed = await run('enrich_articles.js', [{ json: { ...base, url: 'https://long-delay.test/a' } }]);
  assert.ok(Number(process.hrtime.bigint() - started) / 1e6 < 1000, '60초 delay는 실제 1초 이내에 종료');
  assert.strictEqual(sleeps.length, beforeSleeps, '60초 delay는 sleep 없음');
  assert.strictEqual(delayed[0].json.analysis_status, 'title_fallback');
  assert.deepStrictEqual(plain(delayed[0].json.kw), plain(helpers.keywordsOf(base.title)));
  assert.ok(!calls.some(c => c.url === 'https://long-delay.test/a'));
  await assert.rejects(helpers.requestOnce('https://long-delay.test/a', 60000, clock + 40000), /delay/);
  const beforeDeadline = calls.length, deadlineSleeps = sleeps.length;
  await assert.rejects(helpers.requestOnce('https://long-delay.test/a', 10000, clock + 5000), /deadline/);
  assert.strictEqual(calls.length, beforeDeadline, '기한을 넘길 대기 후 요청 금지');
  assert.strictEqual(sleeps.length, deadlineSleeps, '기한을 넘길 대기 금지');
  clock += 1000;
  const chainOrigin = 'https://deadline.test';
  store.robots[chainOrigin] = { rules: [], gap: 10000 };
  for (let i = 0; i < 5; i++) routes.set(`${chainOrigin}/${i}`, {
    status: 302, headers: { location: `/${i + 1}` }, elapsed: 9000,
  });
  const chain = await run('enrich_articles.js', [{ json: { ...base, url: `${chainOrigin}/0` } }]);
  assert.strictEqual(chain[0].json.analysis_status, 'title_fallback');
  assert.strictEqual(calls.filter(c => c.url.startsWith(chainOrigin + '/')).length, 4, '리다이렉트 누적 기한 이후 요청 금지');
  // robots 404/410은 허용, 403/HTML은 불허. 본문 실패 시 기자 메타만 있어도 빈칸으로 복귀.
  for (const status of [404, 410, 403, 200]) {
    const origin = `https://robots-${status}.test`;
    routes.set(origin + '/robots.txt', { status, html: '<html>합성 오류</html>' });
    routes.set(origin + '/a', { html: page() });
    const out = await run('enrich_articles.js', [{ json: { ...base, url: origin + '/a' } }]);
    assert.strictEqual(out[0].json.analysis_status, [404, 410].includes(status) ? 'body_counted' : 'title_fallback');
  }
  for (const route of [
    { html: '<meta name="author" content="홍길동"><article>짧은 속보</article>' },
    { html: page(), headers: { 'content-type': 'application/json' } },
    { bytes: Buffer.alloc(2 * 1024 * 1024 + 1, 65) },
    { bytes: Buffer.from([0xff, 0xfe, 0xff]) },
  ]) {
    routes.set(base.url, route);
    const out = await run('enrich_articles.js', [{ json: base }]);
    assert.strictEqual(out[0].json.analysis_status, 'title_fallback'); assert.strictEqual(out[0].json.byline, '');
  }
  store.pages_opened = 999;
  const lastBudget = await run('enrich_articles.js', [{ json: { ...base, url: 'https://redirect.test/a' } }]);
  assert.strictEqual(lastBudget[0].json.analysis_status, 'title_fallback'); assert.strictEqual(store.pages_opened, 1000);
  const beforeBudget = calls.length;
  await run('enrich_articles.js', [{ json: base }]); assert.strictEqual(calls.length, beforeBudget, '실제 요청 상한도 강제');
  execution = 'run-2'; await run('enrich_articles.js', [{ json: base }]);
  assert.strictEqual(store.run_id, execution); assert.strictEqual(Object.keys(store.robots).length, 1);
  console.log(`OK: 검색어 ${keywords.length}개·8개 그룹·기존 40개 유지·1쪽 우선·호출 상한 및 집계·시간 구간·조기 종료·CSV 5종·본문 미저장`);
}
main().catch(error => { console.error('FAIL: 합성 fixture 검사 실패(본문 원문 출력 생략), 위치: ' + String(error.stack).split('\n').find(line => line.includes('test_preprocess.js:'))); process.exitCode = 1; });
