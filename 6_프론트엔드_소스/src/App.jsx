import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import logoUrl from './assets/dice-logo-vector.svg'
import { folderName, loadFavorites, memberApi } from './account.js'
import { printArticles } from './articlePrint.js'
import {
  articleKey, articleMatchesFilters, articleUrl, downloadArticles, emptyConditions, emptyFilters, eventReports, filterEvents,
  monthDateRange, queryString, readApi, readSearchUrl, resolvedCriteria, searchConditions, searchQuery, uniqueArticles, writeSearchUrl,
} from './search.js'
import './index.css'

const nav = [['search', '검색하기', '⌕'], ['timeline', '타임라인', '◷'], ['trend', '월별 트렌드', 'chart'], ['articles', '관련 기사', '▤'], ['favorites', '북마크', 'bookmark']]
const outline = 'button outline whitespace-nowrap rounded-lg px-[17px] py-3 text-xs font-bold disabled:opacity-40'
const small = 'rounded-md border border-[#ddd] bg-white px-2 py-1 text-xs text-[#444] hover:bg-[#f4f4f4] disabled:opacity-40'
const tag = 'tag whitespace-nowrap rounded-full border border-[#e7ede5] bg-white px-[11px] py-2 text-[13px] text-[#96a198]'
const muted = 'text-[13px] text-[#777]'
const errorText = 'text-sm text-[#b3261e]'
const scopeEmpty = '해당 언론사·기간 기사가 DB에 없습니다.'
const relatedOnly = '질문의 핵심 단어가 모두 들어간 기사가 없어 연관 결과를 보여 드립니다.'
const flowLabels = { AI: 'AI 흐름 요약', 규칙: '제목 기반 흐름', 제목: '대표 제목' }
const number = value => value.toLocaleString('ko-KR')
const monthLabel = month => `${month.slice(0, 4)}년 ${Number(month.slice(5))}월`
const editableConditions = criteria => Object.fromEntries(Object.keys(emptyConditions).map(key => [key, criteria[key] || '']))
const loginIdPattern = /^[A-Za-z0-9_]{4,20}$/
const favoriteBatchSize = 500

function bookmarkArticleIds(articles) {
  return [...new Set(articles.map(article => article.id).filter(id => Number.isInteger(id) && id > 0))]
}

async function saveArticlesAsPdf(articles, options, setNote) {
  setNote('인쇄 창에서 대상: PDF로 저장을 선택하세요')
  try {
    if (!await printArticles(articles, options)) setNote('팝업을 허용하고 PDF 저장을 다시 시도하세요.')
  } catch {
    setNote('인쇄를 시작하지 못했습니다. 다시 시도하세요.')
  }
}

function ChartIcon() { return <svg className="chart-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 4v16h16M7 15l4-4 3 2 5-6" /><path d="M16 7h3v3" /></svg> }
function Bookmark({ filled = false }) { return <svg className="bookmark-icon" viewBox="0 0 24 24" aria-hidden="true" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"><path d="M6 3.5h12v17l-6-4-6 4z" /></svg> }
function LinkIcon({ status }) { return <svg className="bookmark-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{status === '복사됨' ? <path d="M5 12l4 4L19 6" /> : status === '복사 실패' ? <><path d="M12 3L2.5 20h19L12 3z" /><path d="M12 9v4m0 3h.01" /></> : <><path d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" /><path d="M14 11a5 5 0 0 0-7.1 0l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1" /></>}</svg> }
function BrandLogo({ className }) { return <span className={className} role="img" aria-label="DNA"><svg className="dna-login-symbol" viewBox="0 0 430 480" aria-hidden="true"><image href={logoUrl} width="1200" height="480" /></svg><span className="dna-login-word" aria-hidden="true">D<span>N</span>A</span></span> }
function Logo({ onClick }) { return <a className="logo" href="#search" aria-label="DNA 검색하기" onClick={onClick}><BrandLogo className="workspace-dice-logo" /></a> }

function QuickSidebar({ items, active, navigate }) {
  const [position, setPosition] = useState(null)
  const rail = useRef(null), drag = useRef(null)
  function clamp(x, y) { const rect = rail.current?.getBoundingClientRect(); const w = rect?.width || 48, h = rect?.height || 220; return { x: Math.max(0, Math.min(x, Math.max(0, window.innerWidth - w))), y: Math.max(0, Math.min(y, Math.max(0, window.innerHeight - h))) } }
  function down(e) { if (e.button !== 0) return; const rect = rail.current.getBoundingClientRect(); drag.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top }; e.currentTarget.setPointerCapture(e.pointerId) }
  function move(e) { if (drag.current) setPosition(clamp(e.clientX - drag.current.dx, e.clientY - drag.current.dy)) }
  function end() { drag.current = null }
  function key(e) { const delta = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[e.key]; if (!delta) return; e.preventDefault(); const rect = rail.current.getBoundingClientRect(); setPosition(clamp(rect.left + delta[0], rect.top + delta[1])) }
  useEffect(() => { function resize() { setPosition(p => p ? clamp(p.x, p.y) : p) } window.addEventListener('resize', resize); return () => window.removeEventListener('resize', resize) }, [])
  return <aside ref={rail} className="quick-sidebar" aria-label="빠른 메뉴" style={position ? { left: position.x, top: position.y, transform: 'none' } : undefined}>
    <button type="button" className="rail-handle" aria-label="빠른 메뉴 이동: 방향키 또는 드래그" title="드래그하여 빠른 메뉴 이동" onPointerDown={down} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onKeyDown={key}><span aria-hidden="true">⠿</span></button>
    <nav>{items.map(([id, label, icon]) => <a href={'#' + id} key={id} title={label} aria-label={label} className={active === id ? 'selected' : ''} aria-current={active === id ? 'location' : undefined} onClick={e => navigate(e, id)}><span aria-hidden="true">{id === 'favorites' ? <Bookmark filled={active === id} /> : id === 'trend' ? <ChartIcon /> : icon}</span></a>)}</nav>
  </aside>
}

function ArticleLink({ article }) {
  const url = articleUrl(article.url)
  return url ? <a className="underline decoration-[#ccc] underline-offset-4 hover:text-black" href={url} target="_blank" rel="noopener noreferrer">{article.title} <span aria-hidden="true">↗</span></a> : article.title
}
const articleMeta = article => `${article.publisher || '언론사 미상'} · ${article.published_date || '날짜 미상'}`

function MatchReason({ article }) {
  const reason = article.match_reason
  if (!reason) return null
  const labels = []
  if (reason.title?.length) labels.push(`제목: ${reason.title.join('·')}`)
  if (reason.keywords?.length) labels.push(`키워드: ${reason.keywords.join('·')}`)
  const label = labels.join(' / ') || { semantic: '의미 유사', same_event: '동일 보도', followup: '후속 보도' }[reason.basis]
  return label ? <small className="mt-1 block text-xs text-[#888] [overflow-wrap:anywhere]">{label}</small> : null
}

function CopyLinkButton({ article }) {
  const url = articleUrl(article.url)
  const [status, setStatus] = useState('')
  const timer = useRef(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  if (!url) return null
  async function copy() {
    try { await navigator.clipboard.writeText(url); setStatus('복사됨') } catch { setStatus('복사 실패') }
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setStatus(''), 2000)
  }
  const label = `${article.title} 원문 링크 복사`
  return <button type="button" className="star inline-grid !h-7 !w-7 place-items-center !rounded-md" title={label} aria-label={label} onClick={copy}>
    <LinkIcon status={status} /><span className="sr-only" role="status">{status}</span></button>
}

function SaveButton({ article, actions }) {
  const saved = actions.savedIds.has(article.id)
  return <span className="inline-flex flex-wrap gap-1.5">
    <button type="button" className={'star inline-grid !h-7 !w-7 place-items-center !rounded-md ' + (saved ? 'on' : '')} aria-pressed={saved}
      aria-label={`${article.title} ${saved ? '북마크 삭제' : '북마크 추가'}`} title={saved ? '북마크 해제' : '북마크'}
      disabled={actions.busy} onClick={() => actions.onToggle(article, saved)}><Bookmark filled={saved} /></button>
    <CopyLinkButton article={article} /></span>
}

function ReportDetails({ label, rows, actions }) {
  return <details className="event-detail"><summary>{label} 보기 <span>{number(rows.length)}건　⌄</span></summary>
    {rows.length ? <ul>{rows.map(article => <li key={articleKey(article)}>
      <ArticleLink article={article} /><small>{articleMeta(article)}{article.score != null && ` · 관련도 ${article.score.toFixed(2)}`}</small>
      <span className="mt-1 block"><SaveButton article={article} actions={actions} /></span>
    </li>)}</ul> : <p>기사가 없습니다.</p>}
  </details>
}

function Timeline({ data, notice, hasRelatedOnly, error, onRetry, actions }) {
  const years = new Map()
  for (const month of data?.months ?? []) {
    const year = month.month.slice(0, 4)
    if (!years.has(year)) years.set(year, [])
    years.get(year).push(month)
  }
  const eventsByMonth = new Map()
  for (const event of data?.items ?? []) {
    const month = event.date_from.slice(0, 7)
    if (!eventsByMonth.has(month)) eventsByMonth.set(month, [])
    eventsByMonth.get(month).push(event)
  }
  return <section id="timeline" className="section"><div className="section-inner rounded-[14px] bg-white p-8">
    <div className="section-heading flex items-end justify-between gap-4"><div><h2>이슈 타임라인</h2><p>{data?.header || '시간의 흐름에 따라 보도의 맥락을 살펴보세요.'}</p></div><span className={tag}>보도일 기준</span></div>
    <div className="timeline-card overflow-hidden bg-white">
      {error ? <div className="flex flex-wrap items-center gap-3 p-4"><p role="alert" className={errorText}>{error}</p>{onRetry && <button type="button" className={small} onClick={onRetry}>다시 시도</button>}</div>
        : !data ? <p role="status" className={`${muted} p-4`}>{notice || '사건 흐름을 정리하고 있습니다. 잠시 기다려 주세요.'}</p> : <>
          {data.notice && <p role="status" className={`${muted} p-4`}>{hasRelatedOnly && !data.items.length ? '타임라인으로 정리할 사건이 없습니다. 아래 관련 기사에서 연관 결과를 확인할 수 있습니다.' : data.notice}</p>}
          {data.items.length > 0 && data.filter_relaxed && <p className={`${muted} px-4`}>주제와 가까운 사건이 적어 유사도 기준을 완화했습니다.</p>}
          {[...years.entries()].reverse().map(([year, months], index) => <details key={year} open={index === 0} className="border-b border-[#eee] last:border-0">
            <summary className="flex flex-wrap items-baseline gap-3 py-4"><b className="text-[22px] font-extrabold">{year}년</b>
              <span className={muted}>{number(months.length)}개월 · 사건 {number(months.reduce((sum, m) => sum + m.event_count, 0))}개 · 보도 {number(months.reduce((sum, m) => sum + m.article_count, 0))}건</span></summary>
            <div className="pl-2">{months.map(month => <details key={month.month} className="month">
              <summary><span className="month-number">{Number(month.month.slice(5))}월</span><span><b>{month.text}</b><small>{flowLabels[month.source] || month.source} · 사건 {number(month.event_count)}개 · 보도 {number(month.article_count)}건</small></span><i>⌄</i></summary>
              <div className="events">{(eventsByMonth.get(month.month) ?? []).map(event => <article className="event [overflow-wrap:anywhere]" key={event.event_index}>
                <div className="event-dot"></div><div className="event-date">{event.date_label}</div>
                <h3><ArticleLink article={event.representative} /></h3>
                <p>{event.publisher_count > 0 && (event.representative.publisher || event.publishers?.[0]) || event.source_label}
                  {event.publisher_count > 0 && ` · 언론사 ${number(event.publisher_count)}곳`} · 관련 보도 {number(event.articles.length)}건</p>
                <MatchReason article={event.representative} />
                <div className="my-2"><SaveButton article={event.representative} actions={actions} /></div>
                <ReportDetails label="동일 보도" rows={event.articles} actions={actions} />
                <ReportDetails label="후속 기사" rows={event.followups} actions={actions} />
              </article>)}</div>
            </details>)}</div>
          </details>)}
          {data.lead && <details className="ai-summary"><summary>✳ &nbsp; AI 요약 보기 <span>＋</span></summary><p>{data.lead}</p></details>}
          {data.items.length > 0 && <p className={`${muted} p-4`}>기사 제목과 제공된 요약을 바탕으로 정리했습니다. 자세한 내용은 원문을 확인하세요.</p>}
        </>}
    </div>
  </div></section>
}

function Trend({ stats, selectedMonth, onSelectMonth, onClear }) {
  const { monthly } = stats
  const chartRef = useRef(null)
  const [selectedYear, setSelectedYear] = useState('전체')
  const [scrollEnds, setScrollEnds] = useState({ start: true, end: true })
  const years = [...new Set(monthly.map(({ month }) => month.slice(0, 4)))]
  const activeYear = years.includes(selectedYear) ? selectedYear : '전체'
  const visibleMonths = activeYear === '전체' ? monthly : monthly.filter(({ month }) => month.startsWith(activeYear))
  const updateScrollEnds = useCallback(() => {
    const chart = chartRef.current
    if (chart) setScrollEnds({ start: chart.scrollLeft <= 1, end: chart.scrollLeft + chart.clientWidth >= chart.scrollWidth - 1 })
  }, [])
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.scrollLeft = chart.scrollWidth
    updateScrollEnds()
    const observer = new ResizeObserver(updateScrollEnds)
    observer.observe(chart)
    return () => observer.disconnect()
  }, [monthly, activeYear, updateScrollEnds])
  useEffect(() => { setSelectedYear('전체') }, [monthly])
  const peak = Math.max(1, ...visibleMonths.map(month => month.count))
  const total = visibleMonths.reduce((sum, month) => sum + month.count, 0)
  const range = visibleMonths.length ? `${visibleMonths[0].month.replace('-', '.')} — ${visibleMonths.at(-1).month.replace('-', '.')}` : ''
  return <section id="trend" className="section"><div className="section-inner rounded-[14px] bg-white p-8">
    <div className="section-heading flex items-end justify-between gap-4"><div><h2>월별 기사 추이</h2><p>{stats.basis === 'related' ? '연관 결과 기준 · ' : ''}관련 기사 수 {number(total)}건</p></div>{range && <span className={tag}>{range}</span>}</div>
    <div className="chart-card bg-white">
      <div className="chart-title flex flex-wrap items-center justify-between gap-2"><div><span>ARTICLE TREND</span><b>월별 보도량</b></div>
        <select aria-label="연도" value={activeYear} onChange={event => setSelectedYear(event.target.value)}
          className="rounded-md border border-[#ddd] bg-white px-2.5 py-1 text-xs text-[#555]">{['전체', ...years].map(year => <option key={year} value={year}>{year}</option>)}</select></div>
      {monthly.length === 0 ? <p className={`${muted} mt-4`}>집계할 기사가 없습니다.</p> : <>
        <p className="chart-instruction">막대를 선택하면 아래 관련 기사에 해당 월의 기사가 표시됩니다.</p>
        <div className="mt-4 flex min-w-0 items-center gap-2">
          <button type="button" className={`${small} shrink-0`} aria-label="이전 달" disabled={scrollEnds.start} onClick={() => chartRef.current?.scrollBy({ left: -240, behavior: 'smooth' })}>‹</button>
          <div ref={chartRef} onScroll={updateScrollEnds} tabIndex={0} className="min-w-0 flex-1 overflow-x-auto" role="group" aria-label="월별 기사 수 막대 그래프">
            <div className="flex w-full flex-nowrap">{visibleMonths.map(({ month, count }, index) => {
              const selected = selectedMonth === month
              const boundary = index === 0 || month.slice(0, 4) !== visibleMonths[index - 1].month.slice(0, 4)
              const Bar = count > 0 ? 'button' : 'div'
              const control = count > 0 ? { type: 'button', 'aria-label': `${monthLabel(month)} 기사 ${number(count)}건 보기`, 'aria-pressed': selected, onClick: () => onSelectMonth(month, count) } : {}
              return <div key={month} style={{ width: `max(40px, ${100 / Math.min(12, visibleMonths.length)}%)` }} className={`shrink-0 ${boundary ? 'border-l border-[#ddd]' : ''}`}>
                <div className="h-5 pl-1 text-[11px] font-bold text-[#999]" aria-hidden="true">{boundary ? month.slice(0, 4) : ''}</div>
                <Bar {...control} className={`chart-month flex w-full flex-col items-center text-[#888] ${selected ? 'is-selected' : ''}`}>
                  <span className="flex h-[180px] w-full flex-col items-center justify-end gap-1 text-[11px] sm:h-[260px] lg:text-xs">
                    <span>{number(count)}</span>
                    <span aria-hidden="true" className={`bar w-3/5 ${count === peak ? 'highlight' : ''}`} style={{ height: `${count / peak * 85}%` }} />
                  </span>
                  <span aria-hidden="true" className="w-full border-t border-[#e8e8e8] py-2 text-center text-[11px] lg:text-xs">{Number(month.slice(5))}월</span>
                </Bar></div>
            })}</div>
          </div>
          <button type="button" className={`${small} shrink-0`} aria-label="다음 달" disabled={scrollEnds.end} onClick={() => chartRef.current?.scrollBy({ left: 240, behavior: 'smooth' })}>›</button>
        </div>
        {selectedMonth && <div className="month-results !mt-6" aria-live="polite"><div className="month-results-heading"><b>{monthLabel(selectedMonth)} 선택됨</b><button type="button" onClick={onClear}>선택 해제 ×</button></div><p>아래 관련 기사에 이 달의 기사가 표시됩니다.</p></div>}
        <details className="chart-details"><summary>월별 기사 수와 주요 키워드 <span>＋</span></summary><div className="table-wrap"><table><thead><tr><th>월</th><th>기사 수</th><th>주요 키워드</th></tr></thead>
          <tbody>{visibleMonths.map(({ month, count, top_keywords: keywords }) => <tr key={month}><td>{month}</td><td>{number(count)}건</td>
            <td>{(keywords || []).map(word => `${word.keyword} (${number(word.mention_sum)}${word.lift == null ? '' : `, ×${word.lift.toFixed(1)}`})`).join(' · ') || '—'}</td></tr>)}</tbody></table></div>
          <p className={`${muted} mt-2`}>괄호는 월별 언급 수와 평소 대비 배수입니다.</p></details>
      </>}
    </div>
  </div></section>
}

function SearchBox({ ask, setAsk, criteria, conditions, setConditions, onSearch, publishers, status, showIntro }) {
  const [advanced, setAdvanced] = useState(!criteria.ask && Object.keys(emptyConditions).some(key => criteria[key]))
  const setField = (key, value) => setConditions(current => ({ ...current, [key]: value }))
  useEffect(() => { if (!criteria.ask && (criteria.kw || criteria.q)) setAdvanced(true) }, [criteria])
  return <section id="search">
    <div className="hero-top flex items-center justify-center text-center"><div className="hero-title-wrap">{showIntro && <div className="dna-intro" aria-hidden="true"><span className="dna-word"><span>D</span><span className="dna-word-tail">ICE</span></span><span className="dna-word"><span className="dna-initial-n">N</span><span className="dna-word-tail">ews</span></span><span className="dna-word"><span>A</span><span className="dna-word-tail">rchive</span></span></div>}<h1 className={showIntro ? 'intro-title' : undefined}>뉴스를 읽고,<br /><em>흐름을 발견하세요</em></h1></div></div>
    <form className="search-box flex items-center" onSubmit={e => { e.preventDefault(); if (ask.trim()) onSearch({ ask: ask.trim() }) }}>
      <span className="search-icon">⌕</span><label className="sr-only" htmlFor="query">검색어</label>
      <input id="query" value={ask} maxLength={500} onChange={e => setAsk(e.target.value)} placeholder="지난달 삼성전자 주가는 어떻게 움직였어?" />
      <button className="button dark whitespace-nowrap rounded-lg px-[17px] py-3 text-xs font-bold">검색하기 <span>↗</span></button>
    </form>
    <div className="search-meta flex items-center justify-between"><div>시작해 보세요 <button type="button" onClick={() => setAsk('삼성전자 주가')}>삼성전자 주가 ↗</button></div><button type="button" className="text-button" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced} aria-controls="advanced-search">세부 검색 {advanced ? '−' : '+'}</button></div>
    {advanced && <form id="advanced-search" className="advanced" onSubmit={e => { e.preventDefault(); onSearch(conditions) }}>
      <p>질문 대신 조건을 직접 입력해 검색합니다. 필수 키워드나 주제 중 하나는 입력하세요.</p>
      <label>필수 키워드<input value={conditions.kw} maxLength={100} placeholder="반드시 포함할 단어 (예: 삼성전자)" onChange={e => setField('kw', e.target.value)} /></label>
      <label>자연어 주제<input value={conditions.q} maxLength={200} placeholder="알고 싶은 내용 (예: 주가가 왜 떨어졌는지)" onChange={e => setField('q', e.target.value)} /></label>
      <label>언론사<select value={conditions.publisher} onChange={e => setField('publisher', e.target.value)}><option value="">전체 언론사</option>{publishers.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
        {conditions.publisher && !publishers.some(o => o.value === conditions.publisher) && <option value={conditions.publisher}>{conditions.publisher === '__others__' ? '기타 언론사' : conditions.publisher}</option>}</select></label>
      <label>시작일<input type="date" value={conditions.date_from} onChange={e => setField('date_from', e.target.value)} /></label>
      <label>종료일<input type="date" value={conditions.date_to} onChange={e => setField('date_to', e.target.value)} /></label>
      <button type="submit" className="button dark self-end rounded-lg px-[17px] py-3 text-xs font-bold disabled:opacity-40" disabled={!conditions.kw.trim() && !conditions.q.trim()}>조건으로 검색 ↗</button>
    </form>}
    <p className="notice" role="status" aria-live="polite">{status}</p>
  </section>
}

function ArticleCard({ event, index, actions, sort }) {
  const article = event.representative
  const saved = actions.savedIds.has(article.id)
  return <article className="article flex gap-5 [overflow-wrap:anywhere]"><div className="article-index">{String(index + 1).padStart(2, '0')}</div>
    <div className="article-main min-w-0 flex-1">
      <div className="article-meta flex flex-wrap items-center gap-2.5">{sort === 'latest' ? <span className="score">{(event.latest || article.published_date || '날짜 미상').slice(0, 10)}</span> : article.score != null && <span className="score">관련도 {article.score.toFixed(2)}</span>}
        <span>{articleMeta(article)}{event.publisher_count > 0 && ` · 언론사 ${number(event.publisher_count)}곳`}{sort === 'latest' ? article.score != null && ` · 관련도 ${article.score.toFixed(2)}` : event.latest && event.latest !== article.published_date && ` · 최신 일치 ${event.latest}`}</span></div>
      <h3><ArticleLink article={article} /></h3>
      <MatchReason article={article} />
      {article.summary && <p>{article.summary}</p>}
      <ReportDetails label="동일 보도" rows={event.articles} actions={actions} />
      {event.followups.length > 0 && <ReportDetails label="후속 기사" rows={event.followups} actions={actions} />}
    </div>
    <span className="flex shrink-0 flex-col items-center gap-1.5"><button type="button" className={'star ' + (saved ? 'on' : '')} aria-label={saved ? '북마크 삭제' : '북마크 추가'} aria-pressed={saved} disabled={actions.busy} onClick={() => actions.onToggle(article, saved)}><Bookmark filled={saved} /></button><CopyLinkButton article={article} /></span>
  </article>
}

function ArticleResults({ result, question, filters, sort, onDisplayChange, actions, absenceNotice, monthView }) {
  const [note, setNote] = useState('')
  const [shareUrl, setShareUrl] = useState('')
  const [relatedOpen, setRelatedOpen] = useState(false)
  useEffect(() => setRelatedOpen(false), [result, monthView?.month, monthView?.basis])
  const allEvents = useMemo(() => [...result.events, ...(result.related_events || [])], [result])
  const monthData = monthView?.data
  const monthBasis = monthView?.basis
  const scope = monthView?.scope
  const events = useMemo(() => {
    if (!monthBasis) return allEvents
    if (!monthData) return []
    return [...monthData.events, ...(monthData.related_events || [])].filter(event => (event.tier === 'related' ? 'related' : 'core') === monthBasis)
  }, [allEvents, monthBasis, monthData])
  const rows = useMemo(() => filterEvents(events, filters, sort, scope), [events, filters, sort, scope])
  const core = rows.filter(event => event.tier !== 'related')
  const related = rows.filter(event => event.tier === 'related')
  const otherArticles = useMemo(() => uniqueArticles([...(result.other_articles || []), ...(result.excluded_articles || [])]), [result])
  const visibleOther = monthView ? [] : otherArticles.filter(articleMatchesFilters(filters))
  const publisherOptions = useMemo(() => {
    const names = [...new Set([...allEvents, ...events])].flatMap(event => [...eventReports(event), ...(event.followups || [])]).concat(otherArticles).map(a => a.publisher).filter(Boolean)
    return [...new Set([...names, ...filters.within_publisher])].sort((a, b) => a.localeCompare(b, 'ko'))
  }, [allEvents, events, otherArticles, filters.within_publisher])
  const visibleArticles = uniqueArticles(rows.flatMap(event => [event.representative, ...event.articles]))
  const updateFilter = (key, value) => onDisplayChange({ ...filters, [key]: value }, sort)
  const articleResult = monthView ? monthData : result
  const pending = monthView && !monthData && !monthView.error
  const notice = monthView ? monthData?.data_absence_message : absenceNotice
  const nearest = articleResult?.nearest_events || []
  const saveable = uniqueArticles([...visibleArticles, ...rows.flatMap(event => event.followups), ...visibleOther, ...nearest]).filter(a => Number.isInteger(a.id))
  const allSaved = saveable.every(article => actions.savedIds.has(article.id))

  async function copyLink() {
    const url = new URL(location.href)
    url.hash = 'articles'
    try { await navigator.clipboard.writeText(url.href); setShareUrl(''); setNote('검색 조건과 필터가 포함된 링크를 복사했습니다.') }
    catch { setShareUrl(url.href); setNote('아래 공유 링크를 선택해 복사하세요.') }
  }

  function savePdf() {
    const { kw, q, publisher } = searchConditions(result.params)
    const { date_from: from, date_to: to } = scope || result.params
    saveArticlesAsPdf(visibleArticles, {
      name: question || [q, kw].filter(Boolean).join(' '),
      heading: 'DNA 관련 기사',
      details: [
        question && `검색 질문 · ${question}`,
        q && `주제 · ${q}`,
        kw && `키워드 · ${kw}`,
        `언론사 · ${publisher === '__others__' ? '기타 언론사' : publisher || '전체'}`,
        `기간 · ${from || to ? `${from || '처음'} ~ ${to || '현재'}` : '전체'}`,
        filters.within.trim() && `결과 내 포함 단어 · ${filters.within.trim()}`,
        (filters.within_from || filters.within_to) && `결과 내 기간 · ${filters.within_from || '처음'} ~ ${filters.within_to || '현재'}`,
        filters.within_publisher.length > 0 && `결과 내 언론사 · ${filters.within_publisher.join(', ')}`,
      ],
    }, setNote)
  }

  return <section id="articles" className="section"><div className="section-inner rounded-[14px] bg-white p-8">
    <div className="section-heading flex flex-wrap items-end justify-between gap-4"><div>
      <h2 id="article-list-heading" tabIndex={-1}>{monthView ? <>{monthLabel(monthView.month)} 기사 <span>{number(monthView.count)}</span></> : <>관련 기사 <span>{number(visibleArticles.length)}</span></>}</h2>
      {monthView && <p className="month-filter-label">그래프에서 선택한 달의 기사입니다. <button type="button" className="underline" onClick={monthView.onClear}>전체 기간 보기</button></p>}
      <p>AI 관련도는 기사 제목과 검색 주제를 비교한 참고값이며 기사 신뢰도를 의미하지 않습니다.</p></div>
      <div className="actions flex flex-wrap gap-1.5">
        <button type="button" className={outline} disabled={!saveable.length || actions.busy} aria-pressed={saveable.length > 0 && allSaved}
          onClick={() => allSaved ? actions.onRemoveMany(saveable) : actions.onSaveMany(saveable)}><Bookmark filled={saveable.length > 0 && allSaved} /> {saveable.length > 0 && allSaved ? '북마크 해제' : '북마크'}</button>
        <button type="button" className={outline} disabled={!visibleArticles.length} onClick={() => { downloadArticles(visibleArticles); setNote(`표시된 기사 ${number(visibleArticles.length)}건을 CSV로 다운로드합니다.`) }}>↓ CSV</button>
        <button type="button" className={outline} disabled={!visibleArticles.length} onClick={savePdf}>↓ PDF</button>
        <button type="button" className={outline} onClick={copyLink}>↗ 링크</button>
      </div></div>
    {actions.notice && <p className="notice" role="status">{actions.notice}</p>}
    {note && <p className="notice" role="status">{note}</p>}
    {shareUrl && <input className="mt-2 w-full rounded-lg border border-[#ddd] p-2 text-sm" aria-label="공유 링크" readOnly value={shareUrl} onFocus={e => e.target.select()} />}
    {notice ? <p className={`${muted} mt-3`} role="status">{notice}</p> : !result.events.length && events.length > 0 && <p className={`${muted} mt-3`}>{relatedOnly}</p>}
    {nearest.length > 0 && <ul aria-label="선택 기간에서 가장 가까운 기사" className="mt-2 space-y-1 text-sm">{nearest.map(article => <li key={articleKey(article)}><time className="mr-2 text-[#888]">{article.published_date}</time><ArticleLink article={article} /></li>)}</ul>}
    <div className="filter-panel">
      <div className="filter-title flex items-center gap-2.5"><b>결과 내 검색</b><span>제목과 요약에 포함 단어가 모두 있어야 합니다</span><button type="button" onClick={() => onDisplayChange(emptyFilters, sort)}>초기화 ↺</button></div>
      <div className="filters mt-4 grid grid-cols-3 gap-2.5">
        <label>포함 단어<input value={filters.within} maxLength={150} placeholder="예: 반도체 수출" onChange={e => updateFilter('within', e.target.value)} /></label>
        <label>시작일<input type="date" value={filters.within_from} onChange={e => updateFilter('within_from', e.target.value)} /></label>
        <label>종료일<input type="date" value={filters.within_to} onChange={e => updateFilter('within_to', e.target.value)} /></label>
      </div>
      {publisherOptions.length > 0 && <details className="mt-3 rounded-[7px] border border-[#ddd] bg-white px-3 py-2"><summary className="text-sm font-bold text-[#555]">언론사 · {filters.within_publisher.length ? `${number(filters.within_publisher.length)}개 선택` : '전체'}</summary>
        <div className="mt-2 flex max-h-40 flex-wrap gap-x-4 gap-y-2 overflow-y-auto">{publisherOptions.map(publisher => {
          const selected = filters.within_publisher.includes(publisher)
          return <label key={publisher} className="flex items-center gap-2 text-[13px]"><input type="checkbox" checked={selected}
            onChange={() => updateFilter('within_publisher', selected ? filters.within_publisher.filter(v => v !== publisher) : [...filters.within_publisher, publisher])} />{publisher}</label>
        })}</div></details>}
    </div>
    <div className="list-top flex flex-wrap items-center justify-between gap-2"><span>사건 <b>{number(rows.length)}</b>개{monthData && core.length === 20 && monthView.count > core.length && ' · 상위 20개'}</span>
      <label>정렬 <select value={sort} onChange={e => onDisplayChange(filters, e.target.value)}><option value="latest">최신순</option><option value="similarity">관련도순</option></select></label></div>
    {articleResult?.followups_error && <p className={errorText} role="status">{articleResult.followups_error}</p>}
    {pending && <p className={`${muted} p-6`} role="status">이 달의 기사를 불러오고 있습니다.</p>}
    {monthView?.error && <div className="flex flex-wrap items-center gap-3 p-6"><p className={errorText} role="alert">{monthView.error}</p><button type="button" className={small} onClick={monthView.onRetry}>다시 시도</button></div>}
    {core.length > 0 && <div className="articles-list overflow-hidden bg-white" aria-label="핵심 결과">{core.map((event, i) => <ArticleCard key={`${event.grouped}-${event.group_id}`} event={event} index={i} actions={actions} sort={sort} />)}</div>}
    {related.length > 0 && <div className="mt-6"><button type="button" className="text-left text-base font-bold" aria-expanded={relatedOpen} aria-controls="related-results" onClick={() => setRelatedOpen(open => !open)}>
      연관 결과 <span className="text-[#888]">사건 {number(related.length)}건</span> <span aria-hidden="true">{relatedOpen ? '▾' : '▸'}</span></button>
      <div id="related-results" hidden={!relatedOpen}><p className={`${muted} my-2`}>질문의 핵심 단어 중 하나 이상이 일치하거나 주제의 의미가 비슷한 기사입니다.</p>
        <div className="articles-list overflow-hidden bg-white">{related.map((event, i) => <ArticleCard key={`${event.grouped}-${event.group_id}`} event={event} index={core.length + i} actions={actions} sort={sort} />)}</div></div></div>}
    {articleResult && !rows.length && !visibleOther.length && !notice && <div className="empty">{events.length ? '결과 내 검색 조건에 맞는 기사가 없습니다.' : '조건에 맞는 기사가 없습니다.'}</div>}
    {visibleOther.length > 0 && <details className="event-detail mt-6"><summary>다른 기사 보기 <span>{number(visibleOther.length)}건　⌄</span></summary><ul>{visibleOther.map(article => <li key={articleKey(article)}><ArticleLink article={article} /><small>{articleMeta(article)}</small><span className="mt-1 block"><SaveButton article={article} actions={actions} /></span></li>)}</ul></details>}
  </div></section>
}

function FavoriteFolder({ folder, rows, busy, onRename, onDelete, onRemove }) {
  const [mode, setMode] = useState('')
  const [draft, setDraft] = useState('')
  const lastSaved = rows.length ? new Date(rows[0].created_at).toLocaleDateString('ko-KR') : '없음'
  return <details className="month border-b border-[#eee]" open={rows.length > 0 && folder.is_default}>
    <summary><span className="min-w-0"><b>{folder.name}</b><small>{number(rows.length)}건 · 최근 저장일 {lastSaved}{folder.is_default && ' · 기본 폴더'}</small></span><i>⌄</i></summary>
    <div className="pb-5">
      {mode === 'rename' ? <form className="flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); const name = draft.trim(); if (name) onRename(name).then(ok => ok && setMode('')) }}>
        <label className="text-xs font-bold text-[#555]">폴더 이름<input className="mt-1 block rounded-md border border-[#ddd] px-2 py-1.5 text-sm" value={draft} maxLength={100} required autoFocus onChange={e => setDraft(e.target.value)} /></label>
        <button type="submit" className={small} disabled={busy || !draft.trim()}>저장</button><button type="button" className={small} onClick={() => setMode('')}>취소</button>
      </form> : mode === 'delete' ? <div className="rounded-lg bg-[#fff3f0] p-3"><p className="mb-2 text-sm text-[#b3261e]" role="status">‘{folder.name}’ 폴더를 삭제할까요? 저장한 기사 {number(rows.length)}건은 기본 폴더로 옮겨집니다.</p>
        <div className="flex gap-2"><button type="button" className={small} onClick={() => setMode('')} autoFocus>취소</button><button type="button" className={`${small} text-[#b3261e]`} disabled={busy} onClick={onDelete}>폴더 삭제 확인</button></div></div>
        : <div className="flex flex-wrap gap-2"><button type="button" className={small} onClick={() => { setDraft(folder.name); setMode('rename') }}>이름 변경</button>{!folder.is_default && <button type="button" className={small} onClick={() => setMode('delete')}>폴더 삭제</button>}</div>}
      {rows.length ? <ul className="mt-3">{rows.map(row => <li key={row.id} className="flex items-start justify-between gap-3 border-t border-[#eee] py-3 text-sm">
        <div className="min-w-0 [overflow-wrap:anywhere]"><ArticleLink article={row} /><p className="mt-1 text-xs text-[#888]">{articleMeta(row)}{row.saved_query && ` · 검색어 ‘${row.saved_query}’`}</p></div>
        <span className="flex shrink-0 gap-1.5"><CopyLinkButton article={row} /><button type="button" className="star on inline-grid !h-7 !w-7 place-items-center !rounded-md" disabled={busy} title="북마크 해제" aria-label={`북마크에서 ${row.title} 삭제`} onClick={() => onRemove(row)}><Bookmark filled /></button></span></li>)}</ul>
        : <p className={`${muted} mt-3`}>폴더에 저장한 기사가 없습니다.</p>}
    </div>
  </details>
}

function Favorites({ user, favorites, busy, notice, onLogin, onBrowse, onCreate, onRename, onDelete, onRemove }) {
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  const rows = favorites?.rows ?? []
  return <section className="saved" id="favorites"><div className="section-inner rounded-[14px] bg-white p-8"><h1>북마크</h1>
    <div className="section-heading flex flex-wrap items-end justify-between gap-4"><div><h2>저장한 기사 <span>{number(rows.length)}</span></h2><p>{user ? `${user.login_id} 님의 계정에 저장됩니다.` : '로그인하면 회원별로 북마크를 저장합니다.'}</p></div>
      <div className="actions flex flex-wrap gap-1.5">
        <button type="button" className={outline} disabled={!rows.length} onClick={() => saveArticlesAsPdf(rows, { name: '북마크', heading: 'DNA 북마크', details: ['범위 · 전체 폴더'] }, setNote)}>↓ PDF</button>
        <button type="button" className={outline} onClick={onBrowse}>기사 둘러보기 ↗</button>
      </div></div>
    {notice && <p className="notice" role="status">{notice}</p>}
    {note && <p className="notice" role="status">{note}</p>}
    {!user ? <div className="empty"><span><Bookmark /></span><b>로그인이 필요합니다</b><p>로그인하면 검색 결과의 기사를 폴더별로 저장할 수 있어요.</p><button type="button" className="button dark mt-3 rounded-lg px-[17px] py-3 text-xs font-bold" onClick={onLogin}>로그인 / 회원가입</button></div>
      : !favorites ? <p className={`${muted} mt-4`} role="status">북마크를 불러오고 있습니다.</p> : <>
        <form className="mt-4 flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); const value = name.trim(); if (value) onCreate(value).then(ok => ok && setName('')) }}>
          <label className="text-xs font-bold text-[#555]">새 폴더<input className="mt-1 block rounded-md border border-[#ddd] px-2 py-1.5 text-sm" value={name} maxLength={100} placeholder="폴더 이름" onChange={e => setName(e.target.value)} /></label>
          <button type="submit" className={small} disabled={busy || !name.trim()}>폴더 만들기</button></form>
        <div className="articles-list mt-4 overflow-hidden bg-white px-4">{favorites.folders.map(folder => <FavoriteFolder key={folder.id} folder={folder} busy={busy} rows={rows.filter(row => row.folder_id === folder.id)}
          onRename={value => onRename(folder, value)} onDelete={() => onDelete(folder)} onRemove={onRemove} />)}</div>
        {!rows.length && <div className="empty"><span><Bookmark /></span><b>아직 저장한 기사가 없어요</b><p>관심 있는 기사의 북마크를 눌러 모아보세요.</p></div>}
      </>}
  </div></section>
}

function Login({ shared = false, authorization, initialMessage = '', onShared, onLoggedIn }) {
  const [id, setId] = useState(''), [pw, setPw] = useState(''), [confirm, setConfirm] = useState(''), [error, setError] = useState(''), [message, setMessage] = useState(initialMessage), [pending, setPending] = useState(false)
  const [mode, setMode] = useState(shared ? 'shared' : 'login')
  function changeMode(next) { setMode(next); setError(''); setMessage(''); setPw(''); setConfirm('') }
  async function submit(e) {
    e.preventDefault(); setError(''); setMessage('')
    if (mode !== 'shared' && !loginIdPattern.test(id.trim())) { setError('아이디는 영문·숫자·_ 4~20자로 입력해 주세요.'); return }
    if (mode === 'signup' && pw.length < 8) { setError('비밀번호는 8자 이상이어야 합니다.'); return }
    if (mode === 'signup' && pw !== confirm) { setError('비밀번호가 일치하지 않습니다.'); return }
    setPending(true)
    try {
      if (mode === 'shared') await onShared(pw)
      else if (mode === 'signup') {
        await memberApi('auth/signup', { method: 'POST', body: { login_id: id.trim(), password: pw }, authorization })
        changeMode('login'); setMessage('가입되었습니다. 같은 아이디와 비밀번호로 로그인해 주세요.')
      } else {
        const { user } = await memberApi('auth/login', { method: 'POST', body: { login_id: id.trim(), password: pw }, authorization })
        onLoggedIn(user)
      }
    } catch (failure) {
      setError(mode === 'shared' && failure.status === 401 ? '검색 비밀번호를 확인해 주세요.' : failure.message)
    } finally { setPending(false) }
  }
  const title = { shared: '검색 비밀번호', signup: '회원가입', login: '로그인' }[mode]
  return <main className="morph-login"><div className="morph-inner flex w-full max-w-[400px] flex-col items-center"><BrandLogo className="dna-login-logo" /><p className="mb-6 text-center text-[15px] leading-relaxed text-[#666]">뉴스 아카이브에서 이슈의 흐름을 찾는 DNA 워크스페이스입니다.<br />로그인한 회원만 이용할 수 있습니다.</p><div className="morph-shell is-open">
    <section id="login-panel" className="morph-panel" aria-label={title}><header className="morph-header mb-[26px] flex items-start justify-between"><div><span className="morph-kicker">DNA WORKSPACE</span><h1>{title}</h1></div>
</header>
      <form onSubmit={submit}>
        {mode !== 'shared' && <label htmlFor="login-id">아이디<input id="login-id" name="username" value={id} maxLength={20} onChange={e => { setId(e.target.value); setError('') }} autoComplete="username" placeholder="영문·숫자·_ 4~20자" required autoFocus /></label>}
        <label htmlFor="login-pw">비밀번호<input id="login-pw" name="password" type="password" value={pw} maxLength={256} onChange={e => { setPw(e.target.value); setError('') }} autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} placeholder={mode === 'signup' ? '8자 이상' : '비밀번호 입력'} required autoFocus={mode === 'shared'} /></label>
        {mode === 'signup' && <label htmlFor="signup-confirm">비밀번호 확인<input id="signup-confirm" type="password" value={confirm} maxLength={256} onChange={e => { setConfirm(e.target.value); setError('') }} autoComplete="new-password" placeholder="비밀번호 다시 입력" required /></label>}
        {error && <p className="morph-error" role="alert">{error}</p>}{message && <p className="account-message" role="status">{message}</p>}
        <button className={'morph-submit mt-[3px] flex h-[49px] items-center justify-between rounded-xl bg-[#171717] px-[17px] text-[16px] font-bold text-white disabled:opacity-60 ' + (mode === 'login' ? 'login-submit' : '')} type="submit" disabled={pending}>{pending ? '처리 중' : mode === 'signup' ? '회원가입' : mode === 'shared' ? '확인' : '로그인'} <span aria-hidden="true">↗</span></button>
      </form>
      <p className="morph-note">{mode === 'shared' ? '서버에 설정된 공유 검색 비밀번호를 입력하세요. 새로고침하면 다시 입력합니다.' : mode === 'signup' ? '아이디와 비밀번호만 저장합니다. 가입 후 로그인해 주세요.' : '계정이 없으면 아래 회원가입을 눌러 주세요.'}</p>
      {!shared && <button className="account-switch" type="button" onClick={() => changeMode(mode === 'signup' ? 'login' : 'signup')}>{mode === 'signup' ? '로그인으로 돌아가기' : '회원가입'}</button>}
    </section></div></div></main>
}

function Withdrawal({ user, authorization, onClose, onComplete }) {
  const [checked, setChecked] = useState(false), [pw, setPw] = useState(''), [error, setError] = useState(''), [pending, setPending] = useState(false)
  useEffect(() => { function onKey(e) { if (e.key === 'Escape') onClose() } window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey) }, [onClose])
  async function submit(e) {
    e.preventDefault(); setPending(true); setError('')
    try { await memberApi('auth/me', { method: 'DELETE', body: { password: pw }, authorization }); onComplete() }
    catch (failure) { setError(failure.status === 401 ? '비밀번호가 올바르지 않거나 로그인이 만료되었습니다.' : failure.message) }
    finally { setPending(false) }
  }
  return <div className="account-overlay"><form className="withdraw-panel" role="dialog" aria-modal="true" aria-labelledby="withdraw-title" onSubmit={submit}>
    <button className="withdraw-close" type="button" onClick={onClose} aria-label="회원탈퇴 창 닫기">×</button><span className="morph-kicker">DNA WORKSPACE</span><h2 id="withdraw-title">회원탈퇴</h2>
    <p>{user.login_id} 계정을 탈퇴합니다.</p>
    <div className="withdraw-notice">탈퇴하면 계정과 모든 로그인 세션, 북마크 폴더와 저장한 기사가 즉시 삭제되며 되돌릴 수 없습니다.</div>
    <label className="mt-3 block text-sm font-bold text-[#444]">비밀번호 확인<input type="password" className="mt-1 block w-full rounded-lg border border-[#ddd] px-3 py-2" value={pw} maxLength={256} autoComplete="current-password" required onChange={e => { setPw(e.target.value); setError('') }} /></label>
    <label className="withdraw-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} /> 안내 내용을 확인했습니다.</label>
    {error && <p className="morph-error" role="alert">{error}</p>}
    <div className="withdraw-actions"><button type="button" onClick={onClose}>취소</button><button type="submit" disabled={!checked || !pw || pending}>{pending ? '처리 중' : '회원탈퇴'}</button></div>
  </form></div>
}

// 로그인한 회원만 메인 화면에 들어간다. 비로그인·세션 만료(어느 API든 401)면 로그인 화면만 보여 주고,
// URL은 바꾸지 않으므로 로그인 뒤 원래 검색 조건으로 바로 들어간다.
function App() {
  const [shared, setShared] = useState(false)
  const [authorization, setAuthorization] = useState('')
  const [user, setUser] = useState(undefined)  // undefined = 확인 중
  const [boot, setBoot] = useState(null)       // 로그인 후 언론사 목록
  const [notice, setNotice] = useState('')
  const [showIntro, setShowIntro] = useState(false)
  const signOut = useCallback(message => { setUser(null); setBoot(null); setNotice(message || '') }, [])
  const onAuthRequired = useCallback(error => {
    if (error?.basic) setShared(true)
    else signOut('로그인이 만료되었습니다. 다시 로그인해 주세요.')
  }, [signOut])

  useEffect(() => {
    if (shared) return
    let live = true
    memberApi('auth/me', { authorization }).then(data => live && setUser(data.user)).catch(error => {
      if (!live) return
      if (error.basic) setShared(true)
      else { setUser(null); if (error.status !== 401) setNotice(error.message) }
    })
    return () => { live = false }
  }, [shared, authorization])

  useEffect(() => {
    if (!user) return
    const controller = new AbortController()
    readApi('publishers', authorization, controller.signal).then(data => setBoot({ publishers: data.publishers })).catch(error => {
      if (controller.signal.aborted) return
      if (error.status === 401) onAuthRequired(error)
      else setBoot({ publishers: [], warning: '언론사 목록을 불러오지 못했습니다.' })
    })
    return () => controller.abort()
  }, [user, authorization, onAuthRequired])

  useEffect(() => { if (!showIntro) return; const timer = setTimeout(() => setShowIntro(false), 4400); return () => clearTimeout(timer) }, [showIntro])

  async function sharedLogin(password) {
    const bytes = new TextEncoder().encode(`search:${password}`)
    const auth = `Basic ${btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))}`
    await memberApi('auth/me', { authorization: auth }).catch(error => { if (error.status !== 401 || error.basic) throw error })
    setAuthorization(auth)
    setShared(false)
  }

  if (shared) return <Login shared onShared={sharedLogin} />
  if (user === undefined || (user && !boot)) return <main className="morph-login"><p role="status" className="text-[#777]">확인하고 있습니다.</p></main>
  if (!user) return <Login key={notice} authorization={authorization} initialMessage={notice}
    onLoggedIn={next => { setNotice(''); setUser(next); window.scrollTo(0, 0); setShowIntro(true) }} />
  return <Workspace publishers={boot.publishers} warning={boot.warning} authorization={authorization} user={user} showIntro={showIntro}
    onSignOut={signOut} onAuthRequired={onAuthRequired} />
}

function Workspace({ publishers, warning, authorization, user, showIntro, onSignOut, onAuthRequired }) {
  const [initial] = useState(readSearchUrl)
  const [criteria, setCriteria] = useState(initial.criteria)
  const [ask, setAsk] = useState(initial.criteria.ask)
  const [conditions, setConditions] = useState(() => editableConditions(initial.criteria))
  const [filters, setFilters] = useState(initial.filters)
  const [sort, setSort] = useState(initial.sort)
  const [search, setSearch] = useState(null)
  const [selectedMonth, setSelectedMonth] = useState(null)
  const [monthSearch, setMonthSearch] = useState(null)
  const [timeline, setTimeline] = useState(null)
  const [timelineRetry, setTimelineRetry] = useState(0)
  const [stats, setStats] = useState(null)
  const [statsRetry, setStatsRetry] = useState(0)
  const [view, setView] = useState(location.hash === '#favorites' ? 'favorites' : 'search')
  const [active, setActive] = useState(view)
  const [favorites, setFavorites] = useState(null)
  const [favoritesLoading, setFavoritesLoading] = useState(true)
  const [favBusy, setFavBusy] = useState(false)
  const [favNotice, setFavNotice] = useState('')
  const [pendingFavoriteChange, setPendingFavoriteChange] = useState(null)
  const favoriteActionInFlight = useRef(false)
  const [withdrawOpen, setWithdrawOpen] = useState(false)
  const hasSearch = Boolean(criteria.ask || criteria.kw || criteria.q)
  const result = search?.criteria === criteria ? search.data : null
  const error = search?.criteria === criteria ? search.error : ''
  const monthSelection = selectedMonth?.result === result ? selectedMonth : null
  const monthResponse = monthSearch?.selection === monthSelection ? monthSearch : null
  const timelineData = timeline?.result === result && timeline.retry === timelineRetry ? timeline : null
  const statsResponse = stats?.result === result && stats.retry === statsRetry ? stats : null
  const trendStats = result?.stats || statsResponse?.data
  const effective = result ? resolvedCriteria(criteria, result) : criteria
  const favoriteQuery = (criteria.ask || [criteria.kw, criteria.q].filter(Boolean).join(' ')).trim().replace(/\s+/g, ' ').slice(0, 500)

  useEffect(() => {
    if (!hasSearch) return
    const controller = new AbortController()
    readApi(`search?${searchQuery(criteria)}`, authorization, controller.signal).then(data => {
      if (controller.signal.aborted) return
      setSearch({ criteria, data })
      if (data.params.mode !== 'empty') setConditions(searchConditions(data.params))
      if (data.conversion) {
        const display = readSearchUrl()
        writeSearchUrl(resolvedCriteria(criteria, data), display.filters, display.sort, { replace: true })
      }
    }).catch(failure => {
      if (controller.signal.aborted) return
      if (failure.status === 401) onAuthRequired()
      else setSearch({ criteria, error: failure.message })
    })
    return () => controller.abort()
  }, [criteria, hasSearch, authorization, onAuthRequired])

  useEffect(() => {
    if (!result || result.stats || result.params.mode === 'empty') return
    const controller = new AbortController()
    const statsCriteria = result.ai_error ? searchConditions(result.params) : criteria
    readApi(`stats?${queryString({ ...statsCriteria, followups: '1' })}`, authorization, controller.signal).then(data => {
      if (!controller.signal.aborted) setStats({ result, retry: statsRetry, data: data.stats })
    }).catch(failure => {
      if (controller.signal.aborted) return
      if (failure.status === 401) onAuthRequired(failure)
      else setStats({ result, retry: statsRetry, error: failure.message })
    })
    return () => controller.abort()
  }, [result, criteria, statsRetry, authorization, onAuthRequired])

  useEffect(() => {
    if (!monthSelection) return
    const controller = new AbortController()
    readApi(`search?${monthSelection.query}`, authorization, controller.signal).then(data => {
      if (!controller.signal.aborted) setMonthSearch({ selection: monthSelection, data })
    }).catch(failure => {
      if (controller.signal.aborted) return
      if (failure.status === 401) onAuthRequired()
      else setMonthSearch({ selection: monthSelection, error: failure.message })
    })
    return () => controller.abort()
  }, [monthSelection, authorization, onAuthRequired])

  useEffect(() => {
    if (!result || result.ai_error || result.data_absence || result.params.mode === 'empty') return
    if (result.data_absence_message && !result.events.length && !result.related_events?.length) return
    const controller = new AbortController()
    readApi(`timeline?${queryString(resolvedCriteria(criteria, result))}`, authorization, controller.signal).then(data => {
      if (!controller.signal.aborted) setTimeline({ result, retry: timelineRetry, data })
    }).catch(failure => {
      if (controller.signal.aborted) return
      if (failure.status === 401) onAuthRequired()
      else setTimeline({ result, retry: timelineRetry, error: failure.message })
    })
    return () => controller.abort()
  }, [result, criteria, timelineRetry, authorization, onAuthRequired])

  useEffect(() => {
    let active = true
    setFavoritesLoading(true)
    setFavorites(null)
    loadFavorites(authorization).then(next => {
      if (active) setFavorites(next)
    }).catch(failure => {
      if (!active) return
      if (failure.status === 401) onAuthRequired(failure)
      else setFavNotice(failure.message)
    }).finally(() => {
      if (active) setFavoritesLoading(false)
    })
    return () => { active = false }
  }, [authorization, onAuthRequired])

  useEffect(() => {
    function updateView() { setView(location.hash === '#favorites' ? 'favorites' : 'search') }
    function restoreSearch() {
      const restored = readSearchUrl()
      if (queryString(effective) !== queryString(restored.criteria)) {
        setCriteria(restored.criteria); setAsk(restored.criteria.ask); setConditions(editableConditions(restored.criteria)); setSelectedMonth(null)
      }
      setFilters(restored.filters); setSort(restored.sort); updateView()
    }
    window.addEventListener('popstate', restoreSearch)
    window.addEventListener('hashchange', updateView)
    return () => { window.removeEventListener('popstate', restoreSearch); window.removeEventListener('hashchange', updateView) }
  }, [effective])

  useEffect(() => {
    if (view === 'favorites') { setActive('favorites'); return }
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        let current = 'search'
        for (const id of ['search', 'timeline', 'trend', 'articles']) {
          const node = document.getElementById(id)
          if (node && node.getBoundingClientRect().top <= Math.min(window.innerHeight * .35, 200)) current = id
        }
        if (result && window.scrollY > 0 && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 3) current = 'articles'
        setActive(current)
      })
    }
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    document.addEventListener('toggle', update, true)
    update()
    return () => { window.removeEventListener('scroll', update); window.removeEventListener('resize', update); document.removeEventListener('toggle', update, true); cancelAnimationFrame(frame) }
  }, [view, result])

  useEffect(() => { const id = location.hash.slice(1); if (id) document.getElementById(id)?.scrollIntoView() }, [view, result])

  function submitSearch(fields) {
    const next = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, value.trim()]))
    setCriteria(next); setAsk(next.ask || ''); setSelectedMonth(null); setFilters(emptyFilters); setConditions(editableConditions(next))
    writeSearchUrl(next, emptyFilters, sort, { hash: '' })
  }

  function updateDisplay(nextFilters, nextSort) {
    setFilters(nextFilters); setSort(nextSort)
    writeSearchUrl(effective, nextFilters, nextSort, { replace: true })
  }

  function selectMonth(month, count) {
    if (monthSelection?.month === month) { setSelectedMonth(null); return }
    const dates = monthDateRange(month, result.params)
    setSelectedMonth({
      result, month, count, basis: trendStats.basis === 'related' ? 'related' : 'core',
      scope: { ...dates, publisher: result.params.publisher, majorPublishers: new Set(publishers.map(option => option.value)) },
      query: searchQuery({ ...effective, ...dates }),
    })
    requestAnimationFrame(() => document.getElementById('article-list-heading')?.focus())
  }

  function navigate(event, id) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    if (location.hash !== `#${id}`) history.pushState(null, '', `#${id}`)
    setView(id === 'favorites' ? 'favorites' : 'search')
    setActive(id)
    requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' }))
  }

  async function refreshFavorites() {
    const next = await loadFavorites(authorization)
    setFavorites(next)
    return next
  }

  async function memberAction(task, { change = null, refresh = false } = {}) {
    if (favoritesLoading || favoriteActionInFlight.current) return false
    favoriteActionInFlight.current = true
    setFavBusy(true)
    setFavNotice('')
    setPendingFavoriteChange(change)
    let current = favorites
    try {
      current ??= await refreshFavorites()
      const message = await task(current)
      setPendingFavoriteChange(null)
      if (refresh) await refreshFavorites()
      setFavNotice(message)
      return true
    } catch (failure) {
      setPendingFavoriteChange(null)
      if (failure.status === 401) onAuthRequired(failure)
      else {
        setFavNotice(failure.message)
        if (current) await refreshFavorites().catch(error => {
          if (error.status === 401) onAuthRequired(error)
        })
      }
      return false
    } finally {
      favoriteActionInFlight.current = false
      setFavBusy(false)
    }
  }

  function folderAction(path, options, message) {
    return memberAction(async () => {
      await memberApi(path, { ...options, authorization })
      return message
    }, { refresh: true })
  }

  async function queryFolder(folders) {
    const defaultFolder = folders.find(folder => folder.is_default)
    if (!favoriteQuery) return defaultFolder
    const found = folders.find(folder => folder.query === favoriteQuery)
    if (found) return found
    try {
      const { folder } = await memberApi('favorites/folders', { method: 'POST', body: { name: folderName(favoriteQuery), query: favoriteQuery }, authorization })
      setFavorites(current => ({ ...current, folders: [...current.folders, folder] }))
      return folder
    } catch (failure) {
      if (failure.status === 409) return defaultFolder
      throw failure
    }
  }

  function saveArticles(articles, single = false) {
    const ids = bookmarkArticleIds(articles).filter(id => !savedIds.has(id))
    return memberAction(async ({ folders, rows }) => {
      const existing = new Set(rows.map(row => row.article_id))
      const articleIds = ids.filter(id => !existing.has(id))
      if (!articleIds.length) return '이미 북마크에 저장된 기사입니다.'
      const folder = await queryFolder(folders)
      for (let offset = 0; offset < articleIds.length; offset += favoriteBatchSize) {
        const { favorites: savedRows } = await memberApi('favorites/batch', {
          method: 'POST', authorization,
          body: { article_ids: articleIds.slice(offset, offset + favoriteBatchSize), folder_id: folder?.id ?? null, saved_query: favoriteQuery || null },
        })
        setFavorites(current => {
          const byId = new Map(current.rows.map(row => [row.id, row]))
          for (const row of savedRows) byId.set(row.id, row)
          const rows = [...byId.values()].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id)
          return { ...current, rows }
        })
      }
      return single ? `‘${folder?.name || '기본 폴더'}’에 저장했습니다.` : `표시된 기사 ${number(articleIds.length)}건을 북마크에 저장했습니다.`
    }, { change: { ids: new Set(ids), saving: true } })
  }

  function removeFavorites(ids, field, message) {
    const key = field === 'id' ? 'favorite_ids' : 'article_ids'
    return memberAction(async () => {
      const removed = new Set()
      for (let offset = 0; offset < ids.length; offset += favoriteBatchSize) {
        const batch = ids.slice(offset, offset + favoriteBatchSize)
        const { deleted } = await memberApi('favorites/batch-delete', { method: 'POST', body: { [key]: batch }, authorization })
        for (const row of deleted) removed.add(row.article_id)
        // 응답이 성공하면 이미 삭제된 항목도 목록에서 지운다.
        const deletedKeys = new Set(batch)
        setFavorites(current => ({ ...current, rows: current.rows.filter(row => !deletedKeys.has(row[field])) }))
      }
      return message || `표시된 기사 ${number(removed.size)}건의 북마크를 해제했습니다.`
    }, { change: { ids: new Set(ids), field } })
  }

  function toggleFavorite(article, saved) {
    if (!bookmarkArticleIds([article]).length) { setFavNotice('이 기사는 북마크에 저장할 수 없습니다.'); return }
    if (saved) removeFavorites([article.id], 'article_id', '북마크를 해제했습니다.')
    else saveArticles([article], true)
  }

  async function logout() {
    await memberApi('auth/logout', { method: 'POST', authorization }).catch(() => {})
    onSignOut('로그아웃했습니다.')
  }

  const visibleFavorites = useMemo(() => {
    if (!favorites || !pendingFavoriteChange || pendingFavoriteChange.saving) return favorites
    const { ids, field } = pendingFavoriteChange
    return { ...favorites, rows: favorites.rows.filter(row => !ids.has(row[field])) }
  }, [favorites, pendingFavoriteChange])
  const savedIds = useMemo(() => {
    const ids = new Set((visibleFavorites?.rows ?? []).map(row => row.article_id))
    if (pendingFavoriteChange?.saving) for (const id of pendingFavoriteChange.ids) ids.add(id)
    return ids
  }, [visibleFavorites, pendingFavoriteChange])
  const favoritesBusy = favBusy || favoritesLoading
  const actions = {
    savedIds, busy: favoritesBusy, notice: favNotice, onToggle: toggleFavorite,
    onSaveMany: articles => saveArticles(articles),
    onRemoveMany: articles => removeFavorites(bookmarkArticleIds(articles), 'article_id'),
  }
  const hasResults = result && result.params.mode !== 'empty'
  const menu = hasResults ? nav : nav.filter(([id]) => id === 'search' || id === 'favorites')
  const queryLabel = criteria.ask || [result?.params.keywords.join(', '), result?.params.q].filter(Boolean).join(' · ')
  const coverage = result?.data_coverage
  const absenceNotice = result?.data_absence_message || (result?.data_absence === 'scope_empty'
    ? scopeEmpty + (coverage?.date_from && coverage.date_to ? ` 데이터 보유 기간은 ${coverage.date_from}~${coverage.date_to}입니다.` : '') : '')
  const status = hasSearch && !result && !error ? '뉴스를 검색하고 있습니다. 잠시 기다려 주세요.'
    : error || (result?.ai_error ? `${result.ai_error} 세부 검색에서 조건을 직접 입력할 수 있습니다.` : '')
    || (result?.params.mode === 'empty' ? '검색할 키워드나 주제를 입력해 주세요.' : '')
    || (hasResults ? `‘${queryLabel}’ 검색 결과입니다. 아래에서 타임라인과 월별 추이, 관련 기사를 확인하세요.` : '')
    || warning || '질문을 입력하면 필수 키워드, 주제, 기간을 추출해 검색합니다.'
  const monthView = monthSelection && {
    month: monthSelection.month, count: monthSelection.count, basis: monthSelection.basis, scope: monthSelection.scope,
    data: monthResponse?.data, error: monthResponse?.error,
    onClear: () => setSelectedMonth(null), onRetry: () => setSelectedMonth({ ...monthSelection }),
  }

  return <div className="app flex min-h-screen"><QuickSidebar items={menu} active={active} navigate={navigate} /><div className="page">
    {withdrawOpen && user && <Withdrawal user={user} authorization={authorization} onClose={() => setWithdrawOpen(false)}
      onComplete={() => { setWithdrawOpen(false); onSignOut('회원탈퇴가 완료되어 계정과 북마크가 삭제되었습니다.') }} />}
    <header className="topbar"><Logo onClick={e => navigate(e, 'search')} />
      <nav className="header-nav" aria-label="주요 메뉴">{menu.map(([id, label]) => <a key={id} href={'#' + id} onClick={e => navigate(e, id)} className={active === id ? 'selected' : ''} aria-current={active === id ? 'location' : undefined}>{label}</a>)}</nav>
      <div className="top-right flex items-center gap-2.5">{user ? <><span className="avatar">{user.login_id[0].toUpperCase()}</span><span className="user">{user.login_id}<small>개인 워크스페이스</small></span><button type="button" className="top-logout" onClick={logout}>로그아웃</button></> : null}</div>
    </header>
    <nav className="mobile-nav">{menu.map(([id, label]) => <a key={id} href={'#' + id} onClick={e => navigate(e, id)}>{label}</a>)}</nav>
    <main className="content [overflow-wrap:anywhere]">
      {view === 'favorites' ? <Favorites user={user} favorites={visibleFavorites} busy={favoritesBusy} notice={favNotice} onBrowse={e => navigate(e, 'search')}
        onCreate={name => folderAction('favorites/folders', { method: 'POST', body: { name } }, '폴더를 만들었습니다.')}
        onRename={(folder, name) => folderAction(`favorites/folders/${folder.id}`, { method: 'PATCH', body: { name } }, '폴더 이름을 바꿨습니다.')}
        onDelete={folder => folderAction(`favorites/folders/${folder.id}`, { method: 'DELETE' }, '폴더를 삭제하고 기사를 기본 폴더로 옮겼습니다.')}
        onRemove={row => removeFavorites([row.id], 'id', '북마크를 삭제했습니다.')} /> : <>
        <SearchBox ask={ask} setAsk={setAsk} criteria={criteria} conditions={conditions} setConditions={setConditions} onSearch={submitSearch} publishers={publishers} status={status} showIntro={showIntro} />
        {hasResults && <>
          <Timeline data={timelineData?.data} notice={absenceNotice} actions={actions} hasRelatedOnly={!result.events.length && Boolean(result.related_events?.length)}
            error={result.ai_error || timelineData?.error} onRetry={timelineData?.error ? () => setTimelineRetry(count => count + 1) : null} />
          {trendStats ? <Trend stats={trendStats} selectedMonth={monthSelection?.month} onSelectMonth={selectMonth} onClear={() => setSelectedMonth(null)} />
            : <section id="trend" className="section"><div className="section-inner rounded-[14px] bg-white p-8">
              <div className="section-heading"><h2>월별 기사 추이</h2></div>
              {statsResponse?.error
                ? <div className="flex flex-wrap items-center gap-3"><p role="alert" className={errorText}>{statsResponse.error}</p><button type="button" className={small} onClick={() => setStatsRetry(count => count + 1)}>다시 시도</button></div>
                : <p className={muted} role="status">월별 기사와 키워드를 집계하고 있습니다.</p>}
            </div></section>}
          <ArticleResults key={queryString(criteria)} result={result} question={criteria.ask} filters={filters} sort={sort} onDisplayChange={updateDisplay} actions={actions} absenceNotice={absenceNotice} monthView={monthView} />
        </>}
      </>}
      <footer className="footer flex items-center gap-5"><Logo onClick={e => navigate(e, 'search')} /><span>취재의 맥락을 더 깊게. &nbsp; © DICE News Archive</span>{user && <button type="button" className="footer-withdraw" onClick={() => setWithdrawOpen(true)}>회원탈퇴</button>}</footer>
    </main></div></div>
}

export default App
