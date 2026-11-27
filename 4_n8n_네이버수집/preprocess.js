// n8n Code 노드 "전처리·CSV 만들기" (Run Once for All Items)
// 입력: 원문 분석 후 기사별 안전 필드(본문 없음). news_common.js를 앞에 삽입한다.
// 출력: ER다이어그램 테이블별 CSV 파일 5개(binary.data). 본문(description)은 저장하지 않는다.

// 분류 사전과 언론사 매핑은 news_common.js에서 기자명 검증과 공유한다.
function categoriesOf(title, searchKeyword) {
  const primary = Object.hasOwn(KEYWORDS, searchKeyword) ? KEYWORDS[searchKeyword] : null;
  const top = primary ? [{ major: primary[0], minor: primary[1] }] : [];
  const scored = [];
  const titleWithoutAsianGames = title.replaceAll('아시안게임', '');
  for (const [major, minors] of Object.entries(CATEGORIES)) {
    for (const [minor, words] of Object.entries(minors)) {
      if (major === primary?.[0] && minor === primary[1]) continue;
      const score = words.filter(w => (w === '게임' ? titleWithoutAsianGames : title).includes(w)).length;
      if (score) scored.push({ major, minor, score });
    }
  }
  scored.sort((a, b) => b.score - a.score);
  return [...top, ...scored.slice(0, 3 - top.length)];
}
const csvCell = v => {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const toCsv = (cols, rows) => '﻿' + [cols.join(','), ...rows.map(r => cols.map(c => csvCell(r[c])).join(','))].join('\r\n') + '\r\n';

// ---- 1) 루프 완료 출력. ID·구간·적재 시각은 검색 직후 고정되어 자정을 넘어도 변하지 않는다. ----
const input = $input.all();
const ymd = input[0].json.window_id;
const articles = input.filter(it => !it.json.empty).map(it => it.json);
articles.sort((a, b) => new Date(a.published) - new Date(b.published));

// ---- 2) 테이블별 행 만들기 ----
const loadedAt = input[0].json.loaded_at;
const T = { articles: [], article_sources: [], article_keywords: [], article_categories: [], article_groups: [] };
articles.forEach(a => {
  // published_date는 명세서상 DATE → KST 날짜만
  T.articles.push({ id: a.id, source_id: a.source_id, published_date: kst(new Date(a.published)).slice(0, 10), title: a.title, loaded_at: loadedAt, url: a.url });
  T.article_sources.push({ article_id: a.id, publisher: publisherOf(a.url), byline: a.byline || '' });
  for (const [keyword, mention_count] of Object.entries(a.kw)) T.article_keywords.push({ article_id: a.id, keyword, mention_count });
  categoriesOf(a.title, a.keyword).forEach((c, l) =>
    T.article_categories.push({ article_id: a.id, level: l + 1, major: c.major, minor: c.minor }));
});

// ---- 3) 제목 키워드 집합의 Jaccard ≥ 0.5 ----
// 원문 길이·수집 성공 여부에 따라 그룹이 흔들리지 않도록 제목만의 안정된 집합을 사용한다(검색어 제외).
// ponytail: 모든 쌍 비교 O(n²). 하루 수천 건까지는 충분, 그 이상이면 임베딩 + pgvector 검색으로 바꾼다.
const sets = articles.map(a => new Set(Object.keys(keywordsOf(a.title)).filter(w => w !== a.keyword)));
const parent = articles.map((_, i) => i);
const find = i => (parent[i] === i ? i : (parent[i] = find(parent[i])));
for (let i = 0; i < sets.length; i++)
  for (let j = i + 1; j < sets.length; j++) {
    const a = sets[i], b = sets[j];
    if (a.size < 2 || b.size < 2) continue;
    let inter = 0;
    for (const w of a) if (b.has(w)) inter++;
    if (inter / (a.size + b.size - inter) >= 0.5) parent[find(j)] = find(i);
  }
const groups = new Map();
articles.forEach((a, i) => { const r = find(i); if (!groups.has(r)) groups.set(r, []); groups.get(r).push(a); });
let g = 0;
for (const members of groups.values()) {
  if (members.length < 2) continue; // 혼자인 기사는 묶음 없음(0~1 관계)
  const groupId = `${ymd}${pad(++g, 4)}`;
  members.forEach((a, k) => T.article_groups.push({ article_id: a.id, group_id: groupId, is_representative: k === 0 })); // 가장 먼저 발행된 기사가 대표
}

// ---- 4) CSV 파일 5개 (article_embeddings는 AI 미사용이라 만들지 않음) ----
const COLS = {
  articles: ['id', 'source_id', 'published_date', 'title', 'loaded_at', 'url'],
  article_sources: ['article_id', 'publisher', 'byline'],
  article_keywords: ['article_id', 'keyword', 'mention_count'],
  article_categories: ['article_id', 'level', 'major', 'minor'],
  article_groups: ['article_id', 'group_id', 'is_representative'],
};
// 첫 항목의 json.db는 "DB 저장" 노드가 쓰는 적재 자료(n8n id는 매핑용, DB id는 BIGSERIAL). 묶음은 DB 기준과 달라 넣지 않는다.
const db = { articles: T.articles, article_sources: T.article_sources, article_keywords: T.article_keywords, article_categories: T.article_categories };
return Object.entries(COLS).map(([table, cols], i) => ({
  json: { table, rows: T[table].length, ...(i === 0 ? { db } : {}) },
  binary: { data: { data: Buffer.from(toCsv(cols, T[table]), 'utf8').toString('base64'), mimeType: 'text/csv', fileName: `${ymd}_${table}.csv`, fileExtension: 'csv' } },
}));
