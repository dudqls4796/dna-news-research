import { articleUrl } from './search.js'

const fontUrl = new URL('./assets/fonts/PretendardVariable.woff2', import.meta.url).href
const printStyles = `
  @font-face { font-family: Pretendard; src: url("${fontUrl}") format("woff2"); font-weight: 45 920; }
  @page { size: A4; margin: 14mm; }
  body { margin: 0; color: #222; font: 10pt/1.5 Pretendard, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; }
  @media screen { body { padding: 24px; } }
  header { margin-bottom: 12pt; break-after: avoid; overflow-wrap: anywhere; }
  h1 { margin: 0 0 6pt; font-size: 17pt; }
  p { margin: 2pt 0; }
  table { width: 100%; table-layout: fixed; border-collapse: collapse; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  th, td { padding: 5pt; border: 1px solid #bbb; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
  th:nth-child(1) { width: 7%; }
  th:nth-child(2) { width: 38%; }
  th:nth-child(3), th:nth-child(4) { width: 14%; }
  td:last-child { font-size: 9pt; }
  a { color: #0645ad; text-decoration: underline; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
`

function renderPrintDocument(doc, articles, { name, heading, details }) {
  const now = new Date()
  const date = [now.getFullYear(), now.getMonth() + 1, now.getDate()]
    .map(part => String(part).padStart(2, '0')).join('')
  const label = name.replace(/[<>:"/\\|?*\u0000-\u001f]+/g, ' ')
    .trim().replace(/\s+/g, '_').slice(0, 80) || '기사'
  doc.documentElement.lang = 'ko'
  doc.title = `DNA_${label}_${date}`

  function append(parent, tag, text) {
    const node = doc.createElement(tag)
    node.textContent = text
    parent.append(node)
    return node
  }

  append(doc.head, 'style', printStyles)
  const header = append(doc.body, 'header', '')
  append(header, 'h1', heading)
  for (const detail of details.filter(Boolean)) append(header, 'p', detail)
  append(header, 'p', `생성 시각 · ${now.toLocaleString('ko-KR')} / 기사 ${articles.length.toLocaleString('ko-KR')}건`)

  const table = append(doc.body, 'table', '')
  const columns = table.createTHead().insertRow()
  for (const title of ['번호', '제목', '언론사', '발행일', '원문 URL']) append(columns, 'th', title).scope = 'col'

  const body = table.createTBody()
  articles.forEach((article, index) => {
    const row = body.insertRow()
    row.insertCell().textContent = String(index + 1)
    const title = row.insertCell()
    const url = articleUrl(article.url)
    if (url) {
      const link = append(title, 'a', article.title)
      link.href = url
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
    } else {
      title.textContent = article.title
    }
    row.insertCell().textContent = article.publisher || '—'
    row.insertCell().textContent = article.published_date || '—'
    row.insertCell().textContent = url || article.url || '—'
  })
}

export async function printArticles(articles, options) {
  const popup = window.open('about:blank', '_blank')
  if (!popup) return false
  popup.opener = null
  renderPrintDocument(popup.document, articles, options)
  await popup.document.fonts.load('10pt Pretendard').catch(() => {})
  if (!popup.closed) popup.print()
  return true
}
