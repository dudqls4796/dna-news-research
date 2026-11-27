// HTTP 노드는 재시도·리다이렉트 없이 항목당 한 번만 요청한다. 오류 응답도 시도 수에 포함한다.
const MAX_SEARCH_PAGES = 10;
const requestNode = $('검색 요청 만들기');
const responses = $input.all();
const keywordOrder = Object.keys(KEYWORDS);
const { window, apiCalls, skippedKeywords: skippedThisRound } = requestNode.itemMatching(0).json;
const pages = responses.map(({ json }, i) => {
  const { keyword, page, display } = requestNode.itemMatching(i).json;
  const failed = !Array.isArray(json.items);
  const items = failed ? [] : json.items.map(({ originallink, link, pubDate, title }) => ({ originallink, link, pubDate, title }));
  const oldest = Date.parse(items.at(-1)?.pubDate);
  // 시각이 잘못된 한 건 때문에 남은 페이지를 놓치지 않도록, 유효한 시각만 종료 판정에 사용한다.
  const hasMore = items.length === display && !(oldest < window.since);
  return { keyword, page, items, failed, hasNext: hasMore && page < MAX_SEARCH_PAGES,
    possiblyTruncated: page === MAX_SEARCH_PAGES && items.length === display };
}).sort((a, b) => keywordOrder.indexOf(a.keyword) - keywordOrder.indexOf(b.keyword));
const capReached = apiCalls >= SEARCH_API_CALL_CAP;
const hasNext = !capReached && pages.some(page => page.hasNext);
const pairedItem = responses.map((_, item) => ({ item }));
if (hasNext) return [{ json: { window, pages, hasNext, apiCalls }, pairedItem }];

// 마지막 라운드에서 앞선 라운드까지 모은다. 실패가 있어도 성공한 검색 결과는 적재한다.
const resultNode = $('검색 결과 정리');
const allPages = [];
for (let round = 0; round < $runIndex; round++) allPages.push(...resultNode.all(0, round)[0].json.pages);
allPages.push(...pages);
const skipped = new Set(skippedThisRound);
if (capReached) for (const page of pages) if (page.hasNext) skipped.add(page.keyword);
const skippedKeywords = keywordOrder.filter(keyword => skipped.has(keyword));
const truncated = new Set(skippedKeywords);
const pagesByKeyword = Object.fromEntries(keywordOrder.map(keyword => [keyword, 0]));
for (const { keyword, possiblyTruncated } of allPages) {
  pagesByKeyword[keyword]++;
  if (possiblyTruncated) truncated.add(keyword);
}
const byCategory = Object.fromEntries(Object.entries(KEYWORD_GROUPS).map(([category, entries]) => {
  const counts = { apiCalls: 0, possiblyTruncated: 0 };
  for (const [keyword] of entries) {
    counts.apiCalls += pagesByKeyword[keyword];
    if (truncated.has(keyword)) counts.possiblyTruncated++;
  }
  return [category, counts];
}));
const stats = { apiCalls, capReached, skippedKeywords, pagesByKeyword, byCategory,
  possiblyTruncated: keywordOrder.filter(keyword => truncated.has(keyword)),
  failedPages: allPages.filter(p => p.failed).map(({ keyword, page }) => ({ keyword, page })) };
return [{ json: { window, pages: allPages, hasNext, stats }, pairedItem }];
