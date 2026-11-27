// 첫 라운드는 모든 검색어, 이후 라운드는 다음 쪽이 필요한 검색어만 요청한다.
function hourlyWindow(now) {
  const hour = 60 * 60 * 1000;
  const until = Math.floor(now / hour) * hour;
  return { since: until - hour, until,
    window_id: kst(new Date(until)).slice(0, 13).replace(/\D/g, ''), loaded_at: kst(new Date(now)) };
}
const { pages, window = hourlyWindow(Date.now()), apiCalls: usedApiCalls = 0 } = $input.first().json;
const cursors = pages
  ? pages.filter(p => p.hasNext).map(p => ({ keyword: p.keyword, page: p.page + 1 }))
  : Object.keys(KEYWORDS).map(keyword => ({ keyword, page: 1 }));
// 응답 상태와 무관하게 HTTP 노드로 넘기는 요청 수를 먼저 예산에 반영한다.
const requests = cursors.slice(0, Math.max(0, SEARCH_API_CALL_CAP - usedApiCalls));
const apiCalls = usedApiCalls + requests.length;
const skippedKeywords = cursors.slice(requests.length).map(({ keyword }) => keyword);
const DISPLAY = 100;
return requests.map(({ keyword, page }) => ({ json: { keyword, page, display: DISPLAY,
  start: (page - 1) * DISPLAY + 1, window, apiCalls, skippedKeywords }, pairedItem: { item: 0 } }));
