// Code 노드 "원문 대상 만들기". 검색 전에 고정한 한 시간 구간을 그대로 쓴다.
// MAX_PAGES_PER_RUN 초과 기사는 그대로 보존하고 제목만 집계한다.
const { window: { since, until, window_id, loaded_at }, pages, stats } = $input.first().json;
const seenUrls = new Set();
const seenSources = new Set();
const seenArticles = new Set();
const articles = [];
// 네이버 검색 강조는 단어 중간에도 들어가므로 <b> 경계에 공백을 추가하지 않는다.
const cleanTitle = value => clean(String(value || '').replace(/<\/?b\s*>/gi, ''));
// 요청 라운드, 같은 라운드에서는 검색어 정의 순서로 처음 발견한 결과를 사용한다.
for (const { keyword, items } of pages) {
  for (const it of items) {
    const url = it.originallink || it.link;
    const source_id = it.link || url;
    const published = Date.parse(it.pubDate);
    if (!/^https?:\/\//i.test(url || '') || !Number.isFinite(published) || published < since || published >= until ||
        seenUrls.has(url) || seenSources.has(source_id) || isYnaPhoto(url)) continue;
    seenUrls.add(url);
    seenSources.add(source_id);
    const publishedAt = new Date(published);
    const title = cleanTitle(it.title);
    const articleKey = JSON.stringify([kst(publishedAt).slice(0, 10), articleTitleKey(title), publisherOf(url)]);
    if (seenArticles.has(articleKey)) continue;
    seenArticles.add(articleKey);
    articles.push({ keyword, url, source_id, title, published: publishedAt.toISOString(), window_id, loaded_at });
  }
}
articles.sort((a, b) => a.published.localeCompare(b.published));
// 빈 검색에도 루프가 CSV 헤더 5개를 생성할 수 있도록 메타데이터만 전달한다.
if (!articles.length) return [{ json: { empty: true, window_id, loaded_at, stats } }];
return articles.map((a, n) => ({
  json: { ...a, id: `${window_id}${pad(n + 1, 5)}`, open_page: n < MAX_PAGES_PER_RUN, ...(n === 0 ? { stats } : {}) },
  pairedItem: { item: 0 },
}));
