// Code 노드 "원문 분석(본문 미저장)". news_common.js 삽입, 루프 batchSize=1.
// 원문 HTTP 노드를 쓰면 HTML/binary가 실행 이력에 남는다. 요청부터 집계까지 여기서만 수행한다.
// runner 허용 목록: NODE_FUNCTION_ALLOW_BUILTIN=http,https,url (외부 npm 모듈 불필요).
let http, https, URLClass;
try {
  http = require('http'); https = require('https'); URLClass = require('url').URL;
  new TextDecoder('euc-kr');
} catch {
  // 원시 예외에는 응답이 포함될 수 있으므로 정해진 오류 문구만 노출한다.
  throw new Error('원문 분석 실행기 설정 필요: 내장 모듈 http,https,url 허용 및 ICU TextDecoder(euc-kr) 확인');
}
const USER_AGENT = 'DICEArticleCollector/0.2.92 (rule-based keyword statistics; no body storage)';
const REQUEST_TIMEOUT_MS = 10000;
const MAX_BYTES = 2 * 1024 * 1024;
const BASE_GAP_MS = 500; // robots 요청·리다이렉트도 포함해 전체 최대 약 2 req/s.
// n8n runner 제한 60초보다 충분히 짧게: 요청 타임아웃 10초와 robots 조회·리다이렉트를 포함한 기사 처리 상한을 고려한다.
const MAX_WAIT_MS = 20000;
const ARTICLE_DEADLINE_MS = 40000;
const store = $getWorkflowStaticData('node');
// robots 규칙·요청 시각만 캐시한다. 본문/HTML/응답/예외를 staticData에 넣지 않는다.
if (store.run_id !== String($execution.id)) {
  store.run_id = String($execution.id); store.robots = {}; store.next_request = 0; store.domain_last = {}; store.pages_opened = 0;
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const escapeRE = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function parseRobots(text) {
  const groups = [];
  let agents = [], rules = [];
  for (let line of text.split(/\r?\n/)) {
    line = line.split('#')[0].trim();
    const at = line.indexOf(':');
    if (at < 0) continue;
    const key = line.slice(0, at).trim().toLowerCase(), value = line.slice(at + 1).trim();
    if (key === 'user-agent') {
      if (rules.length) { groups.push({ agents, rules }); agents = []; rules = []; }
      agents.push(value.toLowerCase());
    } else if (agents.length && ['allow', 'disallow', 'crawl-delay', 'request-rate'].includes(key)) rules.push([key, value]);
  }
  groups.push({ agents, rules });
  const ranked = groups.map(g => ({ ...g, score: Math.max(-1, ...g.agents
    .filter(a => a === '*' || USER_AGENT.toLowerCase().includes(a)).map(a => a === '*' ? 0 : a.length)) }));
  const best = Math.max(-1, ...ranked.map(g => g.score));
  const selected = ranked.filter(g => g.score === best && best >= 0).flatMap(g => g.rules);
  let gap = BASE_GAP_MS;
  for (const [key, value] of selected) {
    let seconds = NaN;
    if (key === 'crawl-delay') seconds = Number(value);
    if (key === 'request-rate') {
      const [count, duration] = value.split('/').map(Number);
      if (count > 0) seconds = duration / count;
    }
    if (Number.isFinite(seconds) && seconds >= 0) gap = Math.max(gap, seconds * 1000);
  }
  return { rules: selected.filter(([k, v]) => ['allow', 'disallow'].includes(k) && v.startsWith('/')), gap };
}
function robotsAllowed(url, policy) {
  const u = new URLClass(url), path = u.pathname + u.search;
  let length = -1, allowed = true;
  for (const [key, value] of policy.rules) {
    const ending = value.endsWith('$');
    const pattern = (ending ? value.slice(0, -1) : value).split('*').map(escapeRE).join('.*');
    const size = value.replace(/\*/g, '').replace(/\$$/, '').length;
    if (new RegExp('^' + pattern + (ending ? '$' : '')).test(path) &&
        (size > length || (size === length && key === 'allow'))) { length = size; allowed = key === 'allow'; }
  }
  return allowed;
}
async function requestOnce(url, gap = BASE_GAP_MS, deadline) {
  const u = new URLClass(url);
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error('url');
  const wait = Math.max(store.next_request || 0, (store.domain_last[u.origin] || 0) + gap) - Date.now();
  if (wait > MAX_WAIT_MS || gap > MAX_WAIT_MS) throw new Error('delay');
  if (Date.now() + Math.max(0, wait) >= deadline) throw new Error('deadline');
  if (wait > 0) await sleep(wait);
  const remaining = deadline - Date.now();
  if (remaining <= 0) throw new Error('deadline');
  store.next_request = Date.now() + BASE_GAP_MS;
  store.domain_last[u.origin] = Date.now();
  return new Promise((resolve, reject) => {
    let chunks = [], size = 0, settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true; clearTimeout(timer); chunks = [];
      if (error) reject(new Error(error === 'deadline' ? 'deadline' : 'request')); else resolve(value);
    };
    let req;
    const timer = setTimeout(() => { finish(remaining <= REQUEST_TIMEOUT_MS ? 'deadline' : true); req?.destroy(); }, Math.min(REQUEST_TIMEOUT_MS, remaining));
    try {
    req = (u.protocol === 'https:' ? https : http).get(u, {
      headers: { 'User-Agent': USER_AGENT, 'Accept': 'text/html,text/plain;q=0.9', 'Accept-Encoding': 'identity' },
    }, res => {
      const status = res.statusCode, headers = res.headers;
      res.on('error', () => finish(true));
      res.on('aborted', () => finish(true));
      // 리다이렉트/오류 응답의 본문은 버퍼에도 모으지 않는다.
      if (status !== 200) { finish(false, { status, headers, bytes: Buffer.alloc(0) }); res.destroy(); return; }
      if (headers['content-encoding'] && headers['content-encoding'] !== 'identity') {
        res.destroy(); finish(true); return;
      }
      res.on('data', chunk => {
        size += chunk.length;
        if (size > MAX_BYTES) { res.destroy(); finish(true); } else chunks.push(chunk);
      });
      res.on('end', () => finish(false, { status, headers, bytes: Buffer.concat(chunks) }));
    });
    req.on('error', () => finish(true));
    } catch { finish(true); }
  });
}
async function fetchWithRobots(url, deadline, robotsRequest = false) {
  for (let hop = 0; hop < 6; hop++) {
    const u = new URLClass(url);
    if (!['http:', 'https:'].includes(u.protocol)) throw new Error('url');
    let gap = BASE_GAP_MS;
    if (!robotsRequest) {
      if (store.pages_opened >= MAX_PAGES_PER_RUN) throw new Error('cap');
      if (!Object.hasOwn(store.robots, u.origin)) {
        store.robots[u.origin] = null; // 조회 실패는 허용하지 않는다.
        const r = await fetchWithRobots(u.origin + '/robots.txt', deadline, true);
        if ([404, 410].includes(r.status)) store.robots[u.origin] = { rules: [], gap };
        else if (r.status === 200) {
          const text = new TextDecoder('utf-8').decode(r.bytes).replace(/^\uFEFF/, '');
          if (text.trimStart().startsWith('<')) throw new Error('robots');
          store.robots[u.origin] = parseRobots(text);
        }
      }
      const policy = store.robots[u.origin];
      if (!policy || !robotsAllowed(url, policy)) throw new Error('robots');
      gap = policy.gap;
      // requestOnce의 domain_last 기준으로 새 delay를 첫 원문 요청부터 적용한다.
      store.pages_opened++;
    }
    const r = await requestOnce(url, gap, deadline);
    if ([301, 302, 303, 307, 308].includes(r.status)) {
      if (!r.headers.location) throw new Error('redirect');
      url = new URLClass(r.headers.location, url).href;
      continue; // 원문 리다이렉트 목적지도 별도로 robots 검사.
    }
    return { ...r, finalUrl: url };
  }
  throw new Error('redirect');
}
function decodePage(bytes, contentType = '') {
  const sniff = bytes.subarray(0, 8192).toString('latin1');
  const header = contentType.match(/charset\s*=\s*["']?([\w-]+)/i);
  const meta = sniff.match(/<meta\b[^>]*charset\s*=\s*["']?\s*([\w-]+)/i);
  const charset = (header || meta || [null, 'utf-8'])[1];
  return new TextDecoder(charset, { fatal: true }).decode(bytes);
}
function attributes(tag) {
  const out = {};
  for (const m of tag.matchAll(/([^\s=<>/]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g))
    out[m[1].toLowerCase()] = clean(m[2] ?? m[3] ?? m[4]);
  return out;
}
const NON_REPORTER_WORDS = new Set([...REPORTER_TOPIC_WORDS, '기자', '특파원', '논설위원', '편집국', '뉴스', '일보', '신문', '경제', '방송',
  '사회', '정치', '문화', '스포츠', '온라인', '디지털', '콘텐츠', '기고', '취재', '보도', '관련', '대신']);
function validReporter(name, publisher) {
  if (!/^[가-힣]{2,4}$/.test(name) || newsFunctionWord(name) || NON_REPORTER_WORDS.has(name)) return false;
  if (/(?:기자|특파원|논설|편집|뉴스|일보|신문|경제|방송|언론|미디어|취재팀|보도국|협회|재단)/.test(name)) return false;
  // 이름 형태와 겹치기 쉬운 단일 조사(은/이 등)는 일괄 제거하지 않고 복합 조사·용언형을 제한한다.
  if (/(?:하는|되는|라는|이라는|했던|하던|하는데|한다|했다|된다|이다|한|는|된|던|하며|하고|으로|에서|에게|까지|부터|보다|처럼|라고|이며)$/.test(name)) return false;
  const normalized = s => String(s || '').replace(/\s+/g, '').toLowerCase();
  return ![...Object.values(PUBLISHERS), ...[].concat(publisher || [])].some(p => {
    p = normalized(p);
    return p.length >= 2 && (p.includes(name) || name.includes(p));
  });
}
function namesOf(value, publisher = '') {
  if (Array.isArray(value)) return value.flatMap(v => namesOf(v, publisher));
  if (value && typeof value === 'object') {
    const type = [].concat(value['@type'] || 'Person');
    return type.some(t => /(?:^|\/)Person$/.test(t)) ? namesOf(value.name, publisher) : [];
  }
  if (typeof value !== 'string') return [];
  return clean(value).replace(/https?:\/\/\S+|[\w.+-]+@[\w.-]+/gi, ' ')
    .replace(/\([^)]*\)|\[[^\]]*\]/g, ' ').replace(/(?:취재|사진|선임|수석|특파원|기자|기고자)/g, ' ')
    .split(/[,·/;|]|\s+및\s+|\s+and\s+/i).map(s => s.trim())
    .flatMap(s => /^(?:[가-힣]{2,4}\s+)+[가-힣]{2,4}$/.test(s) ? s.split(/\s+/) : [s])
    .filter(s => validReporter(s, publisher));
}
function bylineOf(html, text, publisher = '') {
  const tags = [...html.matchAll(/<meta\b[^>]*>/gi)].map(m => attributes(m[0]));
  const publishers = [publisher, ...tags.filter(a => (a.property || a.name || '').toLowerCase() === 'og:site_name').map(a => a.content)];
  const meta = [];
  for (const a of tags) {
    if (/^(?:article:author|author|dable:author|og:author|og:article:author|dd:author)$/i.test(a.property || a.name || '')) meta.push(...namesOf(a.content, publishers));
  }
  const ld = [];
  const visit = (obj, depth = 0) => {
    if (!obj || typeof obj !== 'object' || depth > 20) return;
    if ([].concat(obj['@type'] || []).some(t => /(?:NewsArticle|Article|ReportageNewsArticle)$/.test(t))) ld.push(...namesOf(obj.author, publishers));
    for (const value of Object.values(obj)) if (value && typeof value === 'object') visit(value, depth + 1);
  };
  for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
    if (/application\/ld\+json/i.test(m[1])) { try { visit(JSON.parse(m[2])); } catch { /* 원문/예외 출력 금지 */ } }
  }
  const patterns = [];
  // 경계 검사로 5자 이상의 일반어 끝 2~4글자를 이름으로 잘라내지 않는다.
  for (const m of text.matchAll(/(?:^|[^가-힣A-Za-z0-9])([가-힣]{2,4}(?:\s*[,·]\s*[가-힣]{2,4})*)\s+(?:(?:선임|수석)\s*)?(?:기자|특파원)(?=$|[^가-힣])/g)) patterns.push(...namesOf(m[1], publishers));
  // 각 출처에서 검증한 결과가 비어 있으면 JSON-LD → 서명 패턴으로 계속 진행한다.
  return [...new Set(meta.length ? meta : ld.length ? ld : patterns)].join(', ');
}
function extractPage(html, publisher = '') {
  // 작은 HTML 스캐너: 중첩 div를 스택으로 처리하고 제거 영역은 하위 텍스트까지 버린다.
  const root = { tag: 'root', attrs: {}, children: [] }, stack = [root], nodes = [];
  const source = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<(script|style|noscript)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
  const tokens = source.matchAll(/<\/?[a-z][^>]*(?:>|$)|[^<]+/gi);
  const voids = new Set(['meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed']);
  for (const m of tokens) {
    const token = m[0];
    if (!token.startsWith('<')) { stack[stack.length - 1].children.push(token); continue; }
    const tag = (token.match(/^<\/?([\w-]+)/) || [])[1]?.toLowerCase();
    if (!tag) continue;
    if (token.startsWith('</')) {
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; }
      continue;
    }
    if (stack[stack.length - 1].tag === 'p' && /^(p|div|section|article|main|h[1-6])$/.test(tag)) stack.pop();
    const parent = stack[stack.length - 1], attrs = attributes(token);
    const node = { tag, attrs, children: [], parent,
      ignored: parent.ignored || /^(nav|header|footer|aside|form|iframe|button|figure|svg|h1)$/.test(tag) ||
        /\b(?:ad|advertisement|related|recommend|banner)\b/i.test(attrs.class || '') || /\bhidden(?:\s|=|>)/i.test(token) };
    parent.children.push(node); nodes.push(node);
    if (stack.length > 256 || nodes.length > 100000) throw new Error('html-size');
    if (!voids.has(tag) && !token.endsWith('/>')) stack.push(node);
  }
  for (const n of [...nodes].reverse().concat(root)) {
    n.text = n.ignored ? '' : n.children.map(c => typeof c === 'string' ? c : c.text).join(' ');
    if (/^(p|div|section|article|main|br)$/.test(n.tag)) n.text = '\n' + n.text + '\n';
  }
  const byline = bylineOf(html, clean(root.text), publisher);
  const rank = n => {
    const id = n.attrs.id || '', cls = n.attrs.class || '';
    // crawler/crawl_news.py BODY_SELECTOR: 동아·경향/국민·매경·한경·세계.
    if (/^(?:articleBody|articletxt|article_txt)$/i.test(id) || /(?:^|\s)(?:news_view|news_cnt_detail_wrap)(?:\s|$)/.test(cls)) return 3;
    if (/articleBody/i.test(n.attrs.itemprop || '') || /article[-_]?body|article[-_]?content|news[-_]?content|dic_area/i.test(id)) return 2;
    return n.tag === 'article' ? 1 : 0;
  };
  const candidates = nodes.filter(n => !n.ignored && rank(n) > 0 && clean(n.text).length >= 40);
  candidates.sort((a, b) => rank(b) - rank(a) || b.text.length - a.text.length);
  let text = candidates[0]?.text || '';
  if (!text) {
    // <p>를 가장 가까운 블록 부모로 모은 뒤 링크 텍스트를 제외한 밀도 점수로 선택한다.
    const groups = new Map();
    for (const p of nodes.filter(n => n.tag === 'p' && !n.ignored)) {
      let parent = p.parent;
      while (parent.parent && !/^(div|section|main|body|article)$/.test(parent.tag)) parent = parent.parent;
      if (!groups.has(parent)) groups.set(parent, []);
      groups.get(parent).push(p.text);
    }
    const ranked = [...groups].map(([parent, paragraphs]) => {
      const joined = paragraphs.join('\n'), chars = clean(joined).length;
      const links = nodes.filter(n => n.tag === 'a' && n.parent === parent).reduce((sum, n) => sum + clean(n.text).length, 0);
      return { joined, score: chars * chars / Math.max(1, clean(parent.text).length + links * 3) };
    }).sort((a, b) => b.score - a.score);
    text = ranked[0]?.joined || '';
  }
  // 여기서 얻은 text/byline은 호출자 메모리 안에서만 사용한다. 노드 반환 금지.
  return { text, byline };
}
function stripBody(text, byline) {
  text = decodeEntities(text); // &copy; 등도 줄 단위 저작권 안내 제거 전에 복원한다.
  text = text.split(/\r?\n/).filter(line => !/(?:copyright|ⓒ|©|무단\s*전재|재배포\s*금지|재전재\s*금지|all rights reserved)/i.test(line)).join(' ')
    .replace(/https?:\/\/[^\s<>]+|www\.[^\s<>]+|[\w.+-]+@[\w.-]+/gi, ' ')
    .replace(/[가-힣]{2,5}\s+(?:(?:선임|수석)\s*)?(?:기자|특파원)/g, ' ');
  for (const name of byline.split(', ').filter(Boolean)) text = text.replace(new RegExp(escapeRE(name), 'g'), ' ');
  return clean(text);
}

const output = [];
for (const [index, item] of $input.all().entries()) {
  const a = item.json;
  if (a.empty) { output.push({ json: { empty: true, window_id: a.window_id, loaded_at: a.loaded_at }, pairedItem: { item: index } }); continue; }
  let kw = keywordsOf(a.title), byline = '', analysis_status = a.open_page ? 'title_fallback' : 'page_cap';
  if (a.open_page) {
    try {
      const response = await fetchWithRobots(a.url, Date.now() + ARTICLE_DEADLINE_MS);
      if (response.status !== 200 || !/^(?:text\/html|application\/xhtml\+xml)(?:;|$)/i.test(response.headers['content-type'] || 'text/html')) throw new Error('html');
      const page = extractPage(decodePage(response.bytes, response.headers['content-type']), publisherOf(a.url));
      const body = stripBody(page.text, page.byline);
      if (body.length < 80) throw new Error('body');
      byline = page.byline;
      kw = keywordsOf(stripBody(a.title, byline) + ' ' + body);
      analysis_status = 'body_counted';
      // response.bytes / HTML / page.text / body는 이 블록을 벗어나면 폐기. 파일·로그·binary·pinData 쓰기 없음.
    } catch { /* 오류·robots 거부·짧은/유료/JS 페이지: 제목 집계 + 빈 기자명, 원시 예외 반환 금지 */ }
  }
  // allowlist 반환: 입력/응답을 spread하지 않아 본문이 다음 노드나 실행 이력으로 넘어가지 않는다.
  output.push({ json: { id: a.id, url: a.url, source_id: a.source_id, title: a.title, keyword: a.keyword,
    published: a.published, window_id: a.window_id, loaded_at: a.loaded_at, kw, byline, analysis_status }, pairedItem: { item: index } });
}
return output;
