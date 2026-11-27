export const emptyConditions = { kw: '', q: '', publisher: '', date_from: '', date_to: '' }
export const emptyFilters = { within: '', within_from: '', within_to: '', within_publisher: [] }
const searchKeys = ['ask', ...Object.keys(emptyConditions), 'mode', 'resolved', 'web_term', 'web_name', 'term_partition']

export function readSearchUrl() {
  const params = new URLSearchParams(location.search)
  const criteria = Object.fromEntries(searchKeys.map(key => [key, params.get(key) || '']))
  const filters = {
    within: params.get('within') || '',
    within_from: params.get('within_from') || '',
    within_to: params.get('within_to') || '',
    within_publisher: [...new Set(params.getAll('within_publisher').filter(Boolean))],
  }
  return { criteria, filters, sort: params.get('sort') === 'similarity' ? 'similarity' : 'latest' }
}

export function queryString(fields) {
  return new URLSearchParams(Object.entries(fields).flatMap(([key, value]) =>
    Array.isArray(value) ? value.map(item => [key, item]) : value ? [[key, value]] : [],
  )).toString()
}

export function searchQuery(criteria) {
  return queryString({ ...criteria, followups: '1', stats: '0' })
}

export function writeSearchUrl(criteria, filters, sort, { replace, hash = location.hash }) {
  const query = queryString({ ...criteria, ...filters, sort })
  const url = `${location.pathname}${query ? `?${query}` : ''}${hash}`
  if (replace) history.replaceState(null, '', url)
  else history.pushState(null, '', url)
}

export function searchConditions(params) {
  return {
    kw: params.keywords.join(', '), q: params.q,
    publisher: params.publisher || '', date_from: params.date_from || '', date_to: params.date_to || '',
  }
}

export function resolvedCriteria(criteria, result) {
  if (!result.conversion) return criteria
  const alias = result.conversion.web_alias
  return {
    ask: criteria.ask, ...searchConditions(result.params), resolved: '1',
    web_term: alias?.term, web_name: alias?.canonical,
    term_partition: result.term_partition ? JSON.stringify(result.term_partition) : '',
  }
}

export function monthDateRange(month, { date_from, date_to }) {
  const first = `${month}-01`
  const [year, index] = month.split('-').map(Number)
  const days = new Date(Date.UTC(year, index, 0)).getUTCDate()
  const last = `${month}-${days}`
  return {
    date_from: date_from && date_from > first ? date_from : first,
    date_to: date_to && date_to < last ? date_to : last,
  }
}

export async function readApi(path, authorization, signal) {
  const headers = authorization ? { Authorization: authorization } : {}
  const response = await fetch(`/api/${path}`, { credentials: 'same-origin', headers, signal }).catch(error => {
    if (signal?.aborted) throw error
    throw new Error('서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.')
  })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data) {
    const error = new Error(data?.error || '서버 응답을 읽지 못했습니다. 잠시 후 다시 시도하세요.')
    error.status = response.status
    error.basic = response.headers.has('www-authenticate')  // SEARCH_PASSWORD 공유 인증 요구(회원 세션 만료와 구분)
    throw error
  }
  return data
}

export function articleUrl(value) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch {
    return ''
  }
}

export function articleKey(article) {
  return String(article.id ?? JSON.stringify([article.url, article.title, article.publisher, article.published_date]))
}

export function uniqueArticles(articles) {
  const unique = new Map()
  for (const article of articles) {
    const key = articleKey(article)
    if (!unique.has(key)) unique.set(key, article)
  }
  return [...unique.values()]
}

export function eventReports(event) {
  return uniqueArticles([event.representative, ...(event.articles?.length ? event.articles : event.matches || [])])
}

export function articleMatchesFilters(filters, scope) {
  const words = value => value.toLocaleLowerCase('ko').trim().split(/\s+/).filter(Boolean)
  const include = words(filters.within)
  const publishers = new Set(filters.within_publisher)
  return article => {
    const compactTitle = article.title.toLocaleLowerCase('ko').replace(/\s+/g, '')
    const summary = (article.summary || '').toLocaleLowerCase('ko')
    const date = article.published_date
    return include.every(word => compactTitle.includes(word) || summary.includes(word)) &&
      (!publishers.size || publishers.has(article.publisher)) &&
      (!filters.within_from || (date && date >= filters.within_from)) &&
      (!filters.within_to || (date && date <= filters.within_to)) &&
      (!scope || (date && date >= scope.date_from && date <= scope.date_to &&
        (!scope.publisher || (scope.publisher === '__others__'
          ? article.publisher && !scope.majorPublishers.has(article.publisher)
          : article.publisher === scope.publisher))))
  }
}

export function filterEvents(events, filters, sort, scope) {
  const matches = articleMatchesFilters(filters, scope)
  const hasFilters = scope || filters.within.trim() || filters.within_publisher.length || filters.within_from || filters.within_to
  const newest = (a, b) => (b.published_date || '').localeCompare(a.published_date || '')
  const byScore = (a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity)
  const rows = []
  for (const event of events) {
    const rep = event.representative
    const reports = eventReports(event).filter(matches)
    const followups = (event.followups || []).filter(matches)
    const preferred = [...(reports.length ? reports : followups)].sort(newest)
    if (!preferred.length) continue
    const latest = hasFilters ? preferred[0].published_date : event.latest_match
    const representative = reports.find(article => articleKey(article) === articleKey(rep)) ||
      (sort === 'similarity' ? preferred.sort(byScore)[0] : preferred[0])
    const scored = articleKey(representative) === articleKey(rep) ? { ...representative, score: event.score } : representative
    rows.push({ ...event, representative: scored, articles: reports, followups, latest })
  }
  return sort === 'similarity' ? rows : rows.sort((a, b) => (b.latest || '').localeCompare(a.latest || ''))
}

export function downloadArticles(articles) {
  const cell = value => {
    let text = String(value ?? '')
    if (/^[\s]*[=+@-]|^[\t\r\n]/.test(text)) text = "'" + text
    return `"${text.replaceAll('"', '""')}"`
  }
  const rows = articles.map(article => [article.title, article.publisher, article.published_date,
    article.score?.toFixed(4), articleUrl(article.url)])
  const csv = [['제목', '언론사', '날짜', '관련도 참고값', '원문 링크'], ...rows]
    .map(row => row.map(cell).join(',')).join('\r\n')
  const url = URL.createObjectURL(new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'dice-관련기사.csv'
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
