"""로컬 전용 목업: python 백엔드/search/app.py → http://127.0.0.1:8010/"""

import argparse
import base64
import binascii
from contextlib import asynccontextmanager
from datetime import date
from difflib import SequenceMatcher
import hmac
import hashlib
from html import escape
import json
from mimetypes import guess_type
import os
from pathlib import Path
import re
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit, urlencode

try:
    from .engine import (SearchError, LLMError, LLMUnavailable, QuestionConversionError, search, start_model_warmup, warm_duplicate_snapshot,
                         generate_timeline, ollama_available, llm_model,
                         clean_month_title, timeline_articles, timeline_month_input, match_timeline_months,
                         event_coverage, timeline_context_label, timeline_month_representative, timeline_params,
                         PERSON_BRANCH_CONTEXTS, REGISTERED_PERSON_NAMES, MAJOR_CATEGORIES,
                         followup_key, FOLLOWUP_WINDOW_DAYS, list_publishers, major_publishers, OTHER_PUBLISHERS)
except ImportError:
    from engine import (SearchError, LLMError, LLMUnavailable, QuestionConversionError, search, start_model_warmup, warm_duplicate_snapshot,
                        generate_timeline, ollama_available, llm_model,
                        clean_month_title, timeline_articles, timeline_month_input, match_timeline_months,
                        event_coverage, timeline_context_label, timeline_month_representative, timeline_params,
                        PERSON_BRANCH_CONTEXTS, REGISTERED_PERSON_NAMES, MAJOR_CATEGORIES,
                        followup_key, FOLLOWUP_WINDOW_DAYS, list_publishers, major_publishers, OTHER_PUBLISHERS)

HOST, PORT = "127.0.0.1", 8010
FRONTEND_DIST = Path(os.environ.get('FRONTEND_DIST', Path(__file__).resolve().parents[2] / '프론트엔드' / 'dist'))
FOLLOWUP_GAP_DAYS = 14
RESULT_FILTERS = {'within': 150, 'without': 150, 'within_from': 10, 'within_to': 10}
DATA_ABSENCE_MESSAGES = {
    'scope_empty': '해당 언론사·기간 기사가 DB에 없습니다.',
}
TIMELINE_SCRIPT = """const slot=document.getElementById('timeline-slot');
if(slot){fetch(slot.dataset.url,{credentials:'same-origin'}).then(async r=>{
if(!r.ok)throw new Error();const html=await r.text();
if(html.trim()){slot.innerHTML=html;}else{slot.querySelector('.hint').textContent='관련 사건이 부족해 타임라인을 만들지 못했습니다(필수 키워드를 줄이거나 바꿔 보세요)';}
}).catch(()=>{const hint=slot.querySelector('.hint');if(hint)hint.textContent='타임라인을 불러오지 못했습니다. 잠시 후 타임라인 보기 링크로 다시 시도하세요.';});}
"""
RESULTS_SCRIPT = """const toolbar = document.getElementById('result-actions');
const favoritesList = document.getElementById('favorites-list');
const saveButton = document.getElementById('save-search');
const storageKey = 'dice-search-favorites';
const status = document.getElementById('result-status');
const refinement = document.getElementById('result-filters');
const filterInputs = Array.from(refinement?.querySelectorAll('[data-param]') || []);
const currentFilters = () => Object.fromEntries(filterInputs.map(field => [field.dataset.param, field.value.trim()]));
let favorites = [];
try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(saved)) {
        favorites = saved.filter(item => typeof item?.label === 'string' &&
            typeof item?.url === 'string' && /^\\/(?:classic)?\\?/.test(item.url))
            .map(item => ({...item, url: location.pathname + item.url.slice(item.url.indexOf('?'))}));
    }
} catch {}
const urlWithDisplayOptions = base => {
    const url = new URL(base, location.origin);
    url.searchParams.set('sort', document.getElementById('result-sort').value);
    for (const [key, value] of Object.entries(currentFilters())) {
        if (value) url.searchParams.set(key, value);
        else url.searchParams.delete(key);
    }
    return url.pathname + url.search;
};
const searchUrl = () => urlWithDisplayOptions(toolbar.dataset.url);
function renderFavorites() {
    favoritesList.replaceChildren();
    for (const item of favorites) {
        const li = document.createElement('li');
        const link = document.createElement('a');
        link.href = item.url;
        link.textContent = item.label;
        li.append(link);
        favoritesList.append(li);
    }
    document.getElementById('favorites-empty').hidden = favorites.length > 0;
    if (saveButton) {
        const saved = favorites.some(item => item.url === searchUrl());
        saveButton.textContent = saved ? '★ 즐겨찾기 해제' : '☆ 즐겨찾기';
        saveButton.setAttribute('aria-pressed', String(saved));
    }
}
renderFavorites();
if (toolbar) {
    const order = document.getElementById('result-sort');
    const results = document.getElementById('results');
    const branchLinks = document.getElementById('search-branches')?.querySelectorAll('a') || [];
    const cards = Array.from(results.children).sort((a, b) => Number(a.dataset.rank) - Number(b.dataset.rank));
    const dateOrder = (a, b) => b.dataset.latest.localeCompare(a.dataset.latest);
    function applyFilters() {
        const filters = currentFilters();
        const words = text => text.toLocaleLowerCase('ko').split(/\\s+/).filter(Boolean);
        const include = words(filters.within);
        const exclude = words(filters.without);
        const filtering = Object.values(filters).some(Boolean);
        const shown = new Set();
        let count = 0;
        for (const card of cards) {
            const rows = Array.from(card.querySelectorAll('[data-csv]'));
            const matching = rows.filter(row => {
                const data = row.dataset;
                const title = data.title.toLocaleLowerCase('ko');
                const inRange = (!filters.within_from || data.date >= filters.within_from) &&
                    (!filters.within_to || (data.date && data.date <= filters.within_to));
                row.hidden = !(inRange &&
                    include.every(word => title.includes(word)) && exclude.every(word => !title.includes(word)));
                return !row.hidden;
            });
            card.hidden = filtering && !matching.length;
            card.dataset.latest = filtering ? matching.reduce((latest, row) =>
                row.dataset.date > latest ? row.dataset.date : latest, '') : card.dataset.originalLatest;
            if (card.hidden) continue;
            count++;
            matching.forEach(row => shown.add(row.dataset.csv));
            const original = card.querySelector('.card-original');
            const heading = card.querySelector('.matched-heading');
            const matchingPeriod = card.querySelector('.matched-count');
            original.hidden = filtering && !matching.some(row => row.dataset.csv === card.dataset.representative);
            heading.hidden = !original.hidden;
            if (original.hidden) {
                const title = matching[0].firstElementChild;
                heading.replaceChildren(title?.matches('a') ? title.cloneNode(true) : matching[0].dataset.title);
            }
            card.querySelector('.original-period').hidden = filtering;
            matchingPeriod.hidden = !filtering;
            if (filtering) matchingPeriod.textContent = `일치 기사 ${matching.length}건`;
        }
        document.getElementById('within-count').textContent = filtering ? `사건 ${count}건 · 기사 ${shown.size}건` : '';
        document.getElementById('within-empty').hidden = !filtering || count !== 0;
        const other = document.getElementById('other-results');
        if (other) other.hidden = filtering;
        const csv = document.getElementById('download-csv');
        if (csv) csv.disabled = count === 0;
    }
    function applyOrder() {
        results.append(...(order.value === 'latest' ? cards.slice().sort(dateOrder) : cards));
        for (const link of branchLinks) link.href = urlWithDisplayOptions(link.dataset.branchUrl);
        renderFavorites();
    }
    order.addEventListener('change', () => {
        applyOrder();
        history.replaceState(null, '', searchUrl());
    });
    function refreshFilters() {
        applyFilters();
        applyOrder();
    }
    if (refinement) {
        refinement.addEventListener('input', refreshFilters);
        refinement.addEventListener('change', () => history.replaceState(null, '', searchUrl()));
        document.getElementById('reset-result-filters').addEventListener('click', () => {
            filterInputs.forEach(field => { field.value = ''; });
            refreshFilters();
            history.replaceState(null, '', searchUrl());
        });
        applyFilters();
    }
    applyOrder();
    saveButton.addEventListener('click', () => {
        const url = searchUrl();
        const existing = favorites.some(item => item.url === url);
        const label = toolbar.dataset.label + ' · ' + order.selectedOptions[0].textContent;
        const updated = existing ? favorites.filter(item => item.url !== url) : [{url, label}, ...favorites];
        try {
            localStorage.setItem(storageKey, JSON.stringify(updated));
            favorites = updated;
            renderFavorites();
            status.textContent = existing ? '즐겨찾기를 해제했습니다.' : '즐겨찾기에 저장했습니다.';
        } catch {
            status.textContent = '브라우저 저장소를 사용할 수 없습니다.';
        }
    });
    document.getElementById('copy-search').addEventListener('click', async () => {
        const link = new URL(searchUrl(), location.origin).href;
        const field = document.getElementById('share-url');
        try {
            await navigator.clipboard.writeText(link);
            status.textContent = '링크를 복사했습니다.';
            field.hidden = true;
        } catch {
            field.value = link;
            field.hidden = false;
            field.select();
            try {
                if (document.execCommand('copy')) {
                    status.textContent = '링크를 복사했습니다.';
                    field.hidden = true;
                    return;
                }
            } catch {}
            status.textContent = '선택된 링크를 복사하세요.';
        }
    });
    const csvButton = document.getElementById('download-csv');
    if (csvButton) csvButton.addEventListener('click', () => {
        const quote = value => {
            const safe = /^[\\s]*[=+@-]|^[\\t\\r\\n]/.test(value) ? "'" + value : value;
            return '"' + safe.replaceAll('"', '""') + '"';
        };
        const rows = [['날짜', '언론사', '제목', '링크', '유사도']];
        const seen = new Set();
        for (const row of results.querySelectorAll('[data-csv]')) {
            if (row.hidden) continue;
            const data = row.dataset;
            if (seen.has(data.csv)) continue;
            seen.add(data.csv);
            rows.push([data.date, data.publisher, data.title, data.url, data.score]);
        }
        const csv = '\\uFEFF' + rows.map(row => row.map(quote).join(',')).join('\\r\\n');
        const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = '뉴스-검색-결과.csv';
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
}
"""
_script_hash = base64.b64encode(hashlib.sha256(TIMELINE_SCRIPT.encode()).digest()).decode()
_results_hash = base64.b64encode(hashlib.sha256(RESULTS_SCRIPT.encode()).digest()).decode()
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{_script_hash}' 'sha256-{_results_hash}'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
FRONTEND_HEADERS = {
    **HEADERS,
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def frontend_file(path: str) -> Path | None:
    if path not in ('/', '/dice-logo-horizontal.png') and not path.startswith('/assets/'):
        return None
    root = FRONTEND_DIST.resolve()
    index = root / 'index.html'
    if not index.is_file() or index.resolve().parent != root:
        return None
    if path == '/':
        return index
    if path == '/dice-logo-horizontal.png':
        directory = root
    elif path.startswith('/assets/') and all(
        re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in ('.', '..') and not part.endswith('.')
        for part in path.removeprefix('/assets/').split('/')
    ):
        directory = root / 'assets'
    else:
        return None
    file = (root / path.lstrip('/')).resolve()
    return file if file.is_relative_to(directory) and file.is_file() else None


def frontend_media_type(file: Path) -> str:
    return {'.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'}.get(file.suffix) or guess_type(file.name)[0] or 'application/octet-stream'
# ponytail: 프로세스별 메모리의 60초 고정 창이다. 재시작·다중 워커 간 공유가 없고
# 경계에서 연속 요청이 가능하다. 전달 IP 헤더는 신뢰하는 터널 경유를 전제로 한다.
_requests: dict[str, tuple[float, int]] = {}
_requests_lock = Lock()


def request_guard(headers, peer: str, counted: bool = True) -> tuple[int, str, dict]:
    client = (headers.get("cf-connecting-ip") or headers.get("x-forwarded-for") or peer).split(",")[0].strip() or peer
    now = monotonic()
    with _requests_lock:
        for key in [key for key, (started, _) in _requests.items() if now - started >= 60]:
            del _requests[key]
        started, count = _requests.get(client, (now, 0))
        if counted:
            _requests[client] = (started, min(count + 1, 21))
    if counted and count >= 20:
        return 429, "요청이 너무 많습니다. 잠시 후 다시 시도하세요.", {
            "Retry-After": str(max(1, int(60 - (now - started)) + 1))}
    password = os.environ.get("SEARCH_PASSWORD")
    if password:
        try:
            scheme, token = headers.get("authorization", "").split(" ", 1)
            decoded = base64.b64decode(token, validate=True)
            _, supplied = decoded.split(b":", 1)
            valid = scheme.lower() == "basic" and hmac.compare_digest(supplied, password.encode("utf-8"))
        except (ValueError, binascii.Error):
            valid = False
        if not valid:
            return 401, "인증이 필요합니다.", {"WWW-Authenticate": 'Basic realm="DICE Search", charset="UTF-8"'}
    return 200, "", {}
STYLE = """
:root{color-scheme:light;--ink:#182a3d;--muted:#53677b;--line:#dbe4eb;--accent:#155d72}
*{box-sizing:border-box}body{margin:0;background:#f1f5f7;color:var(--ink);font:16px/1.65 system-ui,'맑은 고딕',sans-serif}
main{max-width:1080px;margin:auto;padding:40px 24px 64px}.brand{font-size:13px;letter-spacing:.13em;color:var(--accent);font-weight:800}
h1{font-size:clamp(25px,4vw,38px);letter-spacing:-.05em;margin:8px 0}h2{font-size:20px;margin:0 0 18px}
p{margin:8px 0}.intro{color:var(--muted);margin-bottom:26px}.panel,.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;margin-bottom:18px}
label{display:block;font-weight:650;font-size:14px;margin-bottom:14px}input,textarea{display:block;width:100%;margin-top:6px;padding:11px 12px;border:1px solid #b7c7d4;border-radius:8px;background:white;color:var(--ink);font:inherit}
textarea{resize:vertical;min-height:76px}input:focus,textarea:focus{outline:2px solid #3e8ca3;outline-offset:2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}.wide{grid-column:1/-1}.hint,.meta,.summary{color:var(--muted);font-size:14px}
.hint{font-weight:400;display:block;margin-top:4px}button{border:0;border-radius:8px;padding:12px 24px;background:var(--accent);color:#fff;font:inherit;font-weight:700;cursor:pointer}
.actions{display:flex;align-items:center;justify-content:space-between;gap:15px}.results-head{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;margin:30px 0 12px}
.results-head h2{margin:0}.card h3{font-size:20px;line-height:1.5;margin:5px 0 9px;overflow-wrap:anywhere}a{color:var(--ink);text-decoration:none}a:hover{text-decoration:underline;color:var(--accent)}
.eyebrow{color:var(--accent);font-size:12px;font-weight:700}.tags{margin:12px 0;color:var(--muted);font-size:13px}.period{font-size:14px;margin-top:12px}
details{border-top:1px solid var(--line);margin-top:18px;padding-top:12px}summary{cursor:pointer;color:var(--accent);font-weight:600}ol{padding-left:23px}li{margin:15px 0;overflow-wrap:anywhere}
.error{border-left:4px solid #b54832;color:#8b2e1b}.empty{text-align:center;padding:38px 20px;color:var(--muted)}
.search-row{display:flex;align-items:flex-end;gap:14px}.search-row label{flex:1;margin-bottom:0}.search-row button{white-space:nowrap;margin-bottom:1px}
.advanced-search{margin-top:0;padding-top:24px}.advanced-search summary{display:flex;align-items:baseline;gap:12px;min-width:0;list-style:none}.advanced-search summary::-webkit-details-marker{display:none}.advanced-search summary:before{content:'▸';flex:none}.advanced-search[open] summary:before{content:'▾'}.advanced-title{flex:none}.advanced-summary{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:14px;font-weight:400}.advanced-search[open] .advanced-summary{display:none}.advanced-search form{margin-top:18px}
.stats-panel{min-width:0;margin-top:24px}.stats-panel h2{margin-bottom:8px}.stats-panel .hint{overflow-wrap:anywhere}
.monthly-chart{display:block;width:100%;height:auto;margin:16px 0 12px;color:var(--muted)}
.month-bar{fill:var(--accent)}.chart-label{fill:currentColor;text-anchor:middle;font-size:13px}
.chart-baseline{stroke:var(--line)}
.monthly-table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:14px}
.monthly-table th,.monthly-table td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}
.monthly-table .month-cell{width:90px;white-space:nowrap}.monthly-table .count-cell{width:85px;text-align:right;font-variant-numeric:tabular-nums}
.keyword-item{display:inline-block;margin-right:8px;max-width:100%}
.keyword-heading{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;min-width:0}.keyword-guide{min-width:0;max-width:100%;color:var(--muted);font-size:12px;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.timeline{border-top:4px solid var(--accent)}.timeline h2{font-size:26px;line-height:1.4;margin:6px 0 12px}
.timeline .section-label{font-size:14px;font-weight:700;letter-spacing:.04em}.kicker{color:var(--accent);font-size:14px;font-weight:800}
.timeline-flow{font-size:16px}.flow-list{list-style:none;padding:0;margin:6px 0 18px}.flow-list > li{margin:0;border-bottom:1px solid var(--line)}.flow-month-details{border:0;padding:0}
.flow-row{display:grid;grid-template-columns:110px minmax(0,1fr) auto;gap:12px;align-items:baseline;position:relative;padding:9px 0 9px 18px;color:var(--ink);font-weight:400;list-style:none}.flow-row::-webkit-details-marker{display:none}
.flow-marker{position:absolute;left:0;top:9px;color:var(--accent)}.flow-marker::before{content:'▸'}.flow-month-details[open] > .flow-row .flow-marker::before{content:'▾'}
.flow-month{font-weight:700;white-space:nowrap}.flow-label{overflow-wrap:anywhere}.flow-source{font-size:11px;color:var(--muted);white-space:nowrap}.flow-count{font-size:13px;color:var(--muted);white-space:nowrap}
.timeline-ai-summary p{overflow-wrap:anywhere}.timeline-items{list-style:none;padding-left:18px;border-left:2px solid var(--accent)}
.timeline-items > li{position:relative;padding:0 0 10px 8px;margin:18px 0}.timeline-items > li:before{content:'';position:absolute;left:-24px;top:8px;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.source-badge{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:12px;color:var(--accent)}
.timeline-date{color:var(--accent);font-size:13px}.timeline details{margin-top:0}.timeline li h3{font-size:17px;margin:0}
.result-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px}
.result-actions button,.result-actions select{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--accent);font:inherit;width:auto}
.result-actions label{margin:0}.similarity{border-bottom:1px dotted currentColor}
.search-branches{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.search-branches a{padding:5px 12px;border:1px solid var(--line);border-radius:8px}
.search-branches a[aria-current=page]{font-weight:700;border-color:var(--accent)}
.article-list{padding-left:23px}.article-list li{margin:8px 0}.favorites{margin:0 0 18px}
#result-filters{min-width:0}#result-filters select{display:block;margin-top:6px;padding:11px 12px;width:100%;border:1px solid #b7c7d4;border-radius:8px;background:#fff;color:var(--ink);font:inherit}
#result-filters button{align-self:center;justify-self:start}#within-count:empty{display:none}
#share-url[hidden]{display:none}
@media(max-width:640px){.monthly-table{font-size:13px}.monthly-table th,.monthly-table td{padding:8px 4px}.monthly-table .month-cell{width:72px}.monthly-table .count-cell{width:60px}}
@media(max-width:640px){main{padding:24px 14px}.grid{grid-template-columns:1fr}.panel,.card{padding:18px}.actions{align-items:stretch;flex-direction:column}button{width:100%}}
@media(max-width:640px){.search-row{display:block}.search-row label{margin-bottom:14px}.search-row button{margin-top:14px}}
@media(max-width:640px){.flow-row{grid-template-columns:auto minmax(0,1fr);gap:2px 10px}.flow-count{grid-column:2;white-space:normal}}
"""


def esc(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def article_url(article: dict) -> str:
    url = article.get("url") or ""
    try:
        parts = urlsplit(url)
        return url if parts.scheme.lower() in ("http", "https") and parts.netloc else ""
    except ValueError:
        return ""


def title_link(article: dict) -> str:
    url = article_url(article)
    title = esc(article["title"])
    return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{title} ↗</a>' if url else title


SIMILARITY_LABEL = "AI가 산정한 참고용 유사도"


def similarity_label(score) -> str:
    if score is None:
        return ""
    return (f' · <span class="similarity" title="{SIMILARITY_LABEL}입니다. 기사 제목과 검색 주제를 비교한 값입니다.">'
            f'{SIMILARITY_LABEL} {score:.4f}</span>')


def unique_articles(articles: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for article in articles:
        key = article.get('id')
        if key is None:
            key = tuple(article.get(k) for k in ('url', 'published_date', 'publisher', 'title'))
        if key not in seen:
            seen.add(key)
            unique.append(article)
    return unique


def render_article_list(articles: list[dict], *, export: bool = False, after: date | None = None) -> str:
    rows = []
    for article in articles:
        publisher = article.get("publisher") or "언론사 미상"
        day = article.get("published_date") or ""
        if after is not None and day:
            article_date = date.fromisoformat(str(day))
            gap = (article_date - after).days
            if gap >= FOLLOWUP_GAP_DAYS:
                rows.append(f'<li class="hint">다음 보도까지 {gap}일</li>')
            after = article_date
        score = article.get("score")
        attrs = ''
        if export:
            key = article.get('id', (article.get('url'), day, publisher, article['title']))
            score_value = f'{score:.4f}' if score is not None else ''
            attrs = (f' data-csv="{esc(key)}" data-date="{esc(day)}" data-publisher="{esc(publisher)}" '
                     f'data-title="{esc(article["title"])}" data-url="{esc(article_url(article))}" data-score="{score_value}"')
        rows.append(f'<li{attrs}>{title_link(article)}<div class="meta">{esc(publisher)} · '
                    f'{esc(day or "날짜 미상")}{similarity_label(score)}</div></li>')
    return f'<ol class="article-list">{"".join(rows)}</ol>'


def render_card(event: dict, index: int) -> str:
    rep = event["representative"]
    reports = unique_articles([*(event.get('articles') or event['matches']), rep])
    ranking = similarity_label(event['score'])
    latest = event.get('latest_match') or rep['published_date']
    latest_label = f' · 최신 일치 {esc(latest)}' if latest != rep['published_date'] else ''
    return f'''<article class="card" data-rank="{index}" data-latest="{esc(latest)}"
data-original-latest="{esc(latest)}" data-representative="{esc(rep.get('id', ''))}">
<div class="card-original">
<div class="eyebrow">{ranking.removeprefix(' · ')}</div>
<h3>{title_link(rep)}</h3><div class="meta">{esc(rep['publisher'] or '언론사 미상')} · {esc(rep['published_date'])}{latest_label}</div>
<div class="tags">{esc(' / '.join(rep['categories']) or '분류 없음')}</div></div>
<h3 class="matched-heading" hidden></h3><div class="period matched-count" hidden></div>
<div class="period original-period">관련 보도 {len(reports):,}건 · 기간 {esc(event['date_from'])} ~ {esc(event['date_to'])}</div>
<details><summary>동일 보도 보기</summary>{render_article_list(reports, export=True)}</details></article>'''


def render_stats(stats: dict, stats_ms: float = 0) -> str:
    monthly = stats["monthly"]
    total = sum(m["count"] for m in monthly)
    basis = '연관 결과 기준' if stats['basis'] == 'related' else stats['basis']
    aspects = ' · '.join(stats.get('aspects', ()))
    aspect_label = f' · 관점 적용 ({esc(aspects)})' if aspects else ''
    heading = f'''<section class="panel stats-panel" id="monthly-stats" aria-labelledby="monthly-heading">
<h2 id="monthly-heading">월별 기사 수 · 주요 키워드{aspect_label}</h2><p class="hint">{esc(basis)} · {total:,}건</p>'''
    if not monthly:
        return f'{heading}<p class="hint">집계할 기사가 없습니다.</p></section>'
    maximum = max(m["count"] for m in monthly) or 1
    step = 660 / len(monthly)
    columns, rows = [], []
    first_keyword = None
    for i, month in enumerate(monthly):
        x = 30 + (i + .5) * step
        height = month["count"] / maximum * 125
        columns.append(
            f'<g><title>{esc(month["month"])}: {month["count"]:,}건</title>'
            f'<rect class="month-bar" x="{x-step*.3:.2f}" y="{150-height:.2f}" '
            f'width="{step*.6:.2f}" height="{height:.2f}" rx="2"/>'
            f'<text class="chart-label" x="{x:.2f}" y="{143-height:.2f}">{month["count"]:,}</text>'
            f'<text class="chart-label" x="{x:.2f}" y="173">{esc(month["month"][5:])}월</text></g>'
        )
        valid_keywords = [k for k in month["top_keywords"] if k["keyword"][-1:].isalnum()]
        if first_keyword is None:
            first_keyword = next((k for k in valid_keywords
                                  if k.get("lift") is not None and re.search(r'[가-힣A-Za-z0-9].*[가-힣A-Za-z0-9]', k["keyword"])), None)
        keywords = ' · '.join(
            f'<span class="keyword-item">{esc(k["keyword"])}({k["mention_sum"]:,}'
            + (f', ×{k["lift"]:.1f}' if k.get("lift") is not None else '') + ')</span>'
            for k in valid_keywords
        ) or '—'
        if valid_keywords and valid_keywords[0].get('lift') is None:
            keywords += ' <span class="hint">기준 비교 불가 또는 조건 미충족 · 많이 나온 순</span>'
        rows.append(f'<tr><th scope="row" class="month-cell">{esc(month["month"])}</th>'
                    f'<td class="count-cell">{month["count"]:,}</td><td>{keywords}</td></tr>')
    example = ''
    if first_keyword:
        word = esc(first_keyword["keyword"])
        count = f'{first_keyword["mention_sum"]:,}'
        lift = f'{first_keyword["lift"]:.1f}'
        example = f' · 예: {word}({count}, ×{lift}) = 그 달 {count}번 언급, 평소의 {lift}배'
    guide = f'그 달에 평소보다 유난히 많이 나온 단어 최대 4개{example} · 검색어와 흔한 단어는 뺌'
    chart = (f'<svg class="monthly-chart" width="100%" viewBox="0 0 720 185" role="img" '
             f'aria-label="월별 기사 수 막대 그래프. 상세 수치는 아래 표에서 확인할 수 있습니다.">'
             f'<line class="chart-baseline" x1="20" y1="150" x2="700" y2="150"/>'
             f'{"".join(columns)}</svg>')
    return f'''{heading}
<p class="hint">2026-01부터 DB 최근 월까지 · 집계 {stats_ms:,.1f} ms</p>
{chart}<table class="monthly-table" aria-labelledby="monthly-heading">
<thead><tr><th scope="col" class="month-cell">월</th><th scope="col" class="count-cell">기사 수</th><th scope="col"><div class="keyword-heading"><span>주요 키워드</span><span class="keyword-guide" title="{guide}">평소보다 많이 나온 단어 (언급 수, 평소 대비 배수)</span></div></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section>'''


def normalized_title(value: str) -> str:
    return ''.join(ch for ch in value.casefold() if ch.isalnum())


def similar_title(left: str, right: str) -> bool:
    a, b = normalized_title(left), normalized_title(right)
    return bool(a and b and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= .88))


def merge_timeline_items(items: list[dict]) -> list[dict]:
    # 벡터 병합을 마친 항목에는 날짜·제목 기반의 추가 병합을 적용하지 않는다.
    if items and all(i.get('sub_events') for i in items):
        return [{**i, '_articles': [i['representative'], *i.get('matches', [])],
                 '_publishers': set(i.get('publishers', []))} for i in items]
    groups = []
    for original in items:
        item = dict(original)
        item['_ids'] = {item['group_id']} if item.get('group_id') is not None else set()
        item['_keys'] = {(str(item.get('date_from', item['date_label'])),
                          normalized_title(item['representative']['title']))}
        item['_articles'] = [item['representative'], *item.get('matches', [])]
        item['_publishers'] = set(item.get('publishers', []))
        matches = [g for g in groups if g['_ids'] & item['_ids'] or g['_keys'] & item['_keys']]
        while matches:
            for group in matches:
                groups.remove(group)
                item['_ids'].update(group['_ids'])
                item['_keys'].update(group['_keys'])
                item['_articles'] = group['_articles'] + item['_articles']
                item['_publishers'].update(group['_publishers'])
                item['article_count'] = max(item.get('article_count', 1), group.get('article_count', 1))
                item['date_from'] = min(str(item.get('date_from', item['date_label'])), str(group.get('date_from', group['date_label'])))
                item['date_to'] = max(str(item.get('date_to', item['date_label'])), str(group.get('date_to', group['date_label'])))
                if group['representative'].get('publisher') == '동아일보' or item['representative'].get('publisher') != '동아일보':
                    item['representative'] = group['representative']
            matches = [g for g in groups if g['_ids'] & item['_ids'] or g['_keys'] & item['_keys']]
        groups.append(item)
    return sorted(groups, key=lambda i: (str(i.get('date_from', i['date_label'])), i.get('event_index', 0)))


def timeline_notice(message: str, detail: str = '') -> str:
    extra = f'<p class="hint">{esc(detail)}</p>' if detail else ''
    return f'<section class="panel"><h2>사건 타임라인 · 날짜순</h2><p class="hint">{esc(message)}</p>{extra}</section>'


def timeline_reports(item: dict, reports: dict) -> list[dict]:
    if scoped_ids := item.get('_monthly_article_ids'):
        return unique_articles([article for article_id in scoped_ids for article in reports.get((False, article_id), [])])
    return unique_articles([article for sub in item.get('sub_events') or [item]
                            for article in reports.get((sub['grouped'], sub['group_id']), [])])


def timeline_followups(item: dict, followups: dict) -> tuple[list[dict], set]:
    keys = {followup_key(source) for source in [item, *item.get('sub_events', [])]} if followups else set()
    articles = unique_articles([article for key in sorted(keys) for article in followups.get(key, [])])
    articles.sort(key=lambda article: (article['published_date'], article.get('id') or 0))
    return articles, keys


def timeline_period(items: list[dict], first: str | None, last: str | None) -> tuple[str | None, str | None]:
    if items and (not first or not last):
        return (min(str(i.get('date_from', i['date_label'])) for i in items),
                max(str(i.get('date_to', i['date_label'])) for i in items))
    return first, last


def render_monthly_flow(months: list[dict], monthly_events: dict[str, list[tuple[dict, str]]]) -> str:
    summaries = {month['month']: month for month in months}
    rows = []
    previous_year = None
    for key, events in sorted(monthly_events.items()):
        year, number = key.split('-')
        month_label = f'{year}년 {int(number)}월' if previous_year is not None and year != previous_year else f'{int(number)}월'
        previous_year = year
        month = summaries.get(key)
        if month is None:
            representative = timeline_month_representative(item for item, _ in events)['representative']
            month = {'text': clean_month_title(representative['title']), 'source': '제목'}
        source = month['source']
        label, tooltip = {
            'AI': ('흐름 요약', 'AI가 기사 제목과 제공된 요약을 날짜순으로 정리한 흐름'),
            '규칙': ('제목 기반 흐름', '주요 사건의 기사 제목을 날짜순으로 정리한 흐름'),
            '제목': ('대표 제목', '대표 사건의 제목을 정돈한 문구'),
        }[source]
        if month.get('checks'):
            tooltip += ' (AI 흐름 검사: ' + ' / '.join(month['checks']) + ')'
        event_count = sum(item.get('event_count', 1) for item, _ in events)
        article_count = sum(item.get('article_count', 1) for item, _ in events)
        rows.append(f'<li><details class="flow-month-details"><summary class="flow-row">'
                    f'<span class="flow-marker" aria-hidden="true"></span><strong class="flow-month">{esc(month_label)}</strong>'
                    f'<span class="flow-label">{esc(month["text"])} '
                    f'<small class="flow-source" title="{esc(tooltip)}">{esc(label)}</small></span>'
                    f'<span class="flow-count">(사건 {event_count:,} · 보도 {article_count:,}건)</span></summary>'
                    f'<ol class="timeline-items">{"".join(html for _, html in events)}</ol></details></li>')
    return ('<div class="timeline-flow"><strong>흐름</strong><ul class="flow-list">' + ''.join(rows)
            + '</ul><p class="hint flow-notice">기사 제목·제공된 요약 근거 · 원문 확인 필요</p></div>')


def render_timeline(timeline: dict, keywords: list[str], reports: dict | None = None,
                    followups: dict | None = None, *, context_label: str = '',
                    topic_label: str = '',
                    requested_period: tuple[str | None, str | None] = (None, None)) -> str:
    relevance_notice = (f'관련성 판정으로 {timeline["relevance_excluded_count"]:,}건 제외'
                        if timeline.get('relevance_status') == '판정 완료' else '')
    items = merge_timeline_items(timeline["items"])
    if not items:
        if timeline.get('relevance_status') == '판정 실패':
            return timeline_notice('관련성 판정을 완료하지 못했습니다. 잠시 후 다시 시도하세요.')
        if context_label:
            return timeline_notice(f'{context_label} 관련 사건이 없습니다', relevance_notice)
        if timeline.get('no_relevant_events'):
            return timeline_notice('질문과 관련된 사건을 찾지 못했습니다', relevance_notice)
        return timeline_notice('관련 사건이 부족해 타임라인을 만들지 못했습니다(필수 키워드를 줄이거나 바꿔 보세요)')
    monthly_events = {}

    def later_articles(event):
        later, keys = timeline_followups(event, followups or {})
        notice = ''
        if not any(article['publisher'] == '동아일보' for article in later):
            notice = f'<p class="hint">이 기간(종료 후 {FOLLOWUP_WINDOW_DAYS}일) 동아일보 후속 보도 없음</p>'
            if later:
                notice += '<p><strong>다른 언론사 보도</strong></p>'
        return notice + (render_article_list(later, after=date.fromisoformat(min(end for _, end in keys))) if later else '')

    for item in items:
        rep = item['representative']
        start = str(item.get('date_from', item['date_label']))
        end = str(item.get('date_to', start))
        date_label = start if start == end else f'{start} ~ {end}'
        articles = unique_articles(item['_articles']) if reports is None else timeline_reports(item, reports)
        related_count = len(articles)
        details = f'<details><summary>동일 보도 보기</summary>{render_article_list(articles)}</details>'
        later = later_articles(item)
        details += f'<details><summary>후속 기사 보기</summary>{later}</details>'
        publishers = item['_publishers']
        primary = '동아일보' if '동아일보' in publishers else (rep.get('publisher') or next(iter(sorted(publishers)), '언론사 미상'))
        badge = primary + (f' 외 {len(publishers)-1}개 언론사' if len(publishers) > 1 else '')
        row = (f'<li class="timeline-event"><span class="timeline-date">{esc(date_label)}</span> · '
               f'<strong>{title_link(rep)}</strong> · 관련 보도 {related_count:,}건 · '
               f'사건 {item.get("event_count", 1)}개 · '
               f'<span class="source-badge">{esc(badge)}</span>{details}</li>')
        monthly_events.setdefault(start[:7], []).append((item, row))
    kicker = esc(topic_label or context_label or ' · '.join(keywords) or '주제')
    first_date, last_date = timeline_period(items, *requested_period)
    summary = timeline.get('lead', '') if timeline.get('summary_status') in ('통과', '재생성 통과') else ''
    header = f'<h2>{kicker} · {esc(first_date[:7].replace("-", "."))} ~ {esc(last_date[:7].replace("-", "."))}</h2>'
    flow = render_monthly_flow(timeline.get('months') or [], monthly_events)
    ai_summary = (f'<details class="timeline-ai-summary"><summary>AI 요약 보기 '
                  f'({esc(timeline["model"])} · 기사 제목·제공된 본문 요약 근거 · 원문 확인 필요)</summary>'
                  f'<p>{esc(summary)}</p></details>' if summary else '')
    relaxed_notice = ('<p class="hint">주제와 가까운 사건이 적어 유사도 기준을 완화했습니다</p>'
                      if timeline.get('filter_relaxed') else '')
    relevance_detail = f'<p class="hint">{esc(relevance_notice)}</p>' if relevance_notice else ''
    return f'''<section class="panel timeline" aria-label="사건 타임라인 · 날짜순">
<p class="section-label">사건 타임라인 · 날짜순</p>{header}
{relaxed_notice}{relevance_detail}
{flow}
{ai_summary}
<p class="hint">요약 검사: {esc(timeline.get('summary_status', '생략'))} · 전체 LLM {timeline['llm_ms']:,.1f} ms · {esc(timeline['model'])}{' · 캐시' if timeline.get('cached') else ''} · 날짜는 사건 묶음의 보도 기간입니다.</p></section>'''


def result_url(values: dict) -> str:
    fields = {k: values[k] for k in ('ask', 'kw', 'q', 'date_from', 'date_to', 'publisher', 'sort', *RESULT_FILTERS)
              if values.get(k)}
    if fields.get('ask') and (fields.get('kw') or fields.get('q')) and values.get('_resolved'):
        fields['resolved'] = '1'
        if alias := values.get('_web_alias'):
            fields.update(web_term=alias['term'], web_name=alias['canonical'])
        if partition := values.get('_term_partition'):
            fields['term_partition'] = json.dumps(partition, ensure_ascii=False, separators=(',', ':'))
    return values.get('_page_path', '/') + '?' + urlencode(fields)


def timeline_placeholder(result: dict, values: dict | None = None) -> str:
    params = result['params']
    base = {key: val for key, val in (values or {}).items() if key not in RESULT_FILTERS}
    query = result_url({**base, **params, 'kw': ','.join(params['keywords'])}).split('?', 1)[1]
    url = '/timeline?' + query
    return (f'<div id="timeline-slot" data-url="{esc(url + "&fragment=1")}" aria-live="polite">'
            '<section class="panel"><h2>사건 타임라인 · 날짜순</h2><p class="hint">로컬 AI가 정리하고 있습니다 (보통 5~25초)</p>'
            f'<a href="{esc(url)}">타임라인 보기</a><noscript><p>JavaScript가 꺼져 있습니다. 링크를 눌러 확인하세요.</p></noscript>'
            f'</section></div><script>{TIMELINE_SCRIPT}</script>')


def render_result_filters(values: dict) -> str:
    return f'''<fieldset id="result-filters" class="panel"><legend>결과 내 검색</legend>
<div class="grid">
<label>포함 단어<input data-param="within" value="{esc(values.get('within', ''))}" maxlength="150"></label>
<label>제외 단어<input data-param="without" value="{esc(values.get('without', ''))}" maxlength="150"></label>
<label>시작일<input type="date" data-param="within_from" value="{esc(values.get('within_from', ''))}"></label>
<label>종료일<input type="date" data-param="within_to" value="{esc(values.get('within_to', ''))}"></label>
<button type="button" id="reset-result-filters">초기화</button></div>
<p id="within-count" class="hint" role="status"></p></fieldset>'''


def render_person_branches(result: dict, values: dict) -> str:
    params = result['params']
    names = params['keywords']
    if len(names) != 1 or names[0] not in REGISTERED_PERSON_NAMES:
        return ''
    name = names[0]
    branches = result.get('person_branches', [])
    if not branches and params['q'] not in {f'{context} {name}' for context in PERSON_BRANCH_CONTEXTS}:
        return ''

    def link(topic: str, label: str) -> str:
        url = esc(result_url({**values, 'ask': '', 'kw': name, 'q': topic, **{
            key: params.get(key) for key in ('date_from', 'date_to', 'publisher')}}))
        current = ' aria-current="page"' if params['q'] == topic else ''
        return f'<a href="{url}" data-branch-url="{url}"{current}>{esc(label)}</a>'

    links = [link(branch['topic'], f'{branch["topic"]} {branch["count"]:,}건') for branch in branches]
    links.append(link('', '전체'))
    return '<nav id="search-branches" class="search-branches" aria-label="인물 맥락 선택">' + ''.join(links) + '</nav>'


def render_page(values: dict, result=None, error=None) -> str:
    def value(key):
        return esc(values.get(key, ""))

    action = esc(values.get('_page_path', '/'))
    fields = ('kw', 'q', 'date_from', 'date_to', 'publisher')
    has_conditions = any(values.get(key, '').strip() for key in fields)
    advanced_open = has_conditions and not values.get('ask', '').strip()
    period = (f"{values.get('date_from') or '처음'} ~ {values.get('date_to') or '현재'}"
              if values.get('date_from') or values.get('date_to') else '전체')
    conditions = []
    if values.get('kw', '').strip():
        conditions.append(f"필수 키워드 {values['kw']}")
    if values.get('q', '').strip():
        conditions.append(f"주제 {values['q']}")
    if has_conditions:
        publisher_label = '기타 언론사' if values.get('publisher') == OTHER_PUBLISHERS else values.get('publisher') or '전체'
        conditions.extend((f'기간 {period}', f'언론사 {publisher_label}'))
    condition_summary = esc(' · '.join(conditions) or '조건 없음')
    selected_publisher = values.get('publisher', '')
    publishers = values.get('_publishers', [])
    major = major_publishers(publishers)
    def option_group(label, names):
        options = ''.join(f'<option value="{esc(name)}"{" selected" if name == selected_publisher else ""}>'
                          f'{esc(name)}</option>' for name in names)
        return f'<optgroup label="{label}">{options}</optgroup>'
    publisher_options = option_group('주요 언론사', major)
    publisher_options += (f'<option value="{OTHER_PUBLISHERS}"'
                          f'{" selected" if selected_publisher == OTHER_PUBLISHERS else ""}>기타 언론사</option>')
    if selected_publisher and selected_publisher != OTHER_PUBLISHERS and selected_publisher not in major:
        publisher_options += (f'<option value="{esc(selected_publisher)}" selected>'
                              f'{esc(selected_publisher)}</option>')

    ai_error = result.get('ai_error') if result else None
    hint = (ai_error or '로컬 AI 연결 안 됨 · 기존 필수 키워드와 자연어 주제로 검색하세요.'
            if ai_error or not values.get('_ai_available', True) else
            f'로컬 AI({llm_model()})가 질문을 검색 조건으로 바꿉니다. 아래 조건을 고쳐 재검색할 수 있습니다.')
    conversion = result.get('conversion') if result else None
    conversion_html = ''
    sort = values.get('sort', 'latest')
    if conversion:
        removed = ' · '.join(f'{r["keyword"]}({r["reason"]})' for r in conversion['removed_keywords']) or '없음'
        web_query = conversion.get('web_query')
        alias = conversion.get('web_alias')
        web_detail = f"{alias['term']} → {alias['canonical']}" if alias else web_query
        web_status = f'사용({esc(web_detail)})' if web_query else '미사용'
        if not web_query and (reason := conversion.get('web_reason')):
            web_status += f'({esc(reason)})'
        conversion_html = (f'<p class="hint">변환: 필수 키워드 {esc(", ".join(conversion["keywords"]) or "없음")} · '
                           f'주제 {esc(conversion["q"])} · 기간 {esc(conversion.get("date_from") or "제한 없음")} ~ '
                           f'{esc(conversion.get("date_to") or "제한 없음")} · 언론사 {esc(conversion.get("publisher") or "전체")} · 제거: {esc(removed)} · '
                           f'LLM {conversion["llm_ms"]:,.1f} ms{" · 캐시" if conversion.get("cached") else ""} · '
                           f'웹 확인: {web_status}</p>')
    body = ""
    if error:
        body = f'<div class="panel error" role="alert">{esc(error)}</div>'
    elif result is not None:
        events = result['events'] + result.get('related_events', [])
        count = len(events)
        data_absence = result.get('data_absence_message') or DATA_ABSENCE_MESSAGES.get(result.get('data_absence'))
        has_context = result.get('person_context') or result.get('national_team_sports')
        no_context_match = has_context and not count
        if no_context_match and not data_absence and not any(month['count'] for month in result['stats']['monthly']):
            body = timeline_notice(f'{result["params"]["q"]} 관련 사건이 없습니다')
        elif not data_absence:
            body = (timeline_notice('로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다')
                    if ai_error or (not result['params'].get('q') and not values.get('_ai_available', True)) else
                    timeline_placeholder(result, values) if result['params'].get('q') or count else
                    timeline_notice('관련 사건이 부족해 타임라인을 만들지 못했습니다(필수 키워드를 줄이거나 바꿔 보세요)'))
        body += render_stats(result['stats'], result['timings'].get('stats_ms', 0))
        digest = result['params']['mode'] == 'digest'
        has_topic = bool(result['params'].get('q')) and not digest
        relaxed = result.get('result_filter_relaxed', False)
        cutoff = (f" · 기본 유사도 기준 {result['result_min_cos']:g}"
                  if has_topic and not relaxed else "")
        excluded = f" · 제외 {result.get('excluded_candidate_count', 0):,}건" if has_topic and not relaxed else ""
        label = ' · '.join(values[k] for k in ('kw', 'q') if values.get(k))
        label = values.get('ask') or label or '검색 조건'
        if no_context_match:
            heading = f'{result["params"]["q"]} 기사를 찾지 못했습니다'
        elif has_context or result.get('excluded_categories') or result.get('target_category'):
            heading = f'{label} 관련 기사 {count:,}건'
        else:
            heading = f'검색 결과 · 사건 {count:,}건'
        body += f'''<div class="results-head"><h2>{esc(heading)}</h2>
<span class="summary">후보 {result['candidate_count']:,}건{excluded} · {result['elapsed_ms']:,.1f} ms</span></div>
<p class="hint">관련 보도와 기간은 지정한 기간·언론사 조건 안에서 집계합니다{cutoff}.</p>'''
        if relaxed:
            body += '<p class="hint">주제와 가까운 기사가 적어 필수 키워드 기준으로 보여 드립니다</p>'
        if result['params']['mode'] == 'combined':
            cap = (f"후보 상한 {result['candidate_limit']:,}건 적용" if result['candidate_limit_applied']
                   else "후보 상한 미적용")
            body += (f'<p class="hint">필수 키워드 일치 {result["keyword_candidate_count"]:,}건 · '
                     f'정확 거리 평가 {result["ranked_candidate_count"]:,}건 · {cap}</p>')
        csv_button = '<button type="button" id="download-csv">CSV 다운로드</button>' if events else ''
        body += f'''<div class="result-actions" id="result-actions" data-url="{esc(result_url({**values, 'sort': sort}))}" data-label="{esc(label)}">
<label for="result-sort">정렬</label><select id="result-sort">
<option value="similarity"{' selected' if sort == 'similarity' else ''}>{'주요 사건순' if digest else '유사도순'}</option>
<option value="latest"{' selected' if sort == 'latest' else ''}>최신순</option>
</select><button type="button" id="copy-search">링크 복사</button>
<button type="button" id="save-search" aria-pressed="false">☆ 즐겨찾기</button>{csv_button}</div>
<p id="result-status" class="hint" role="status"></p>
<input id="share-url" aria-label="공유 링크" readonly hidden>'''
        body += render_person_branches(result, values)
        if count:
            body += render_result_filters(values)
            body += '<div id="within-empty" class="panel empty" hidden>결과 내 검색 조건에 맞는 기사가 없습니다.</div>'
        ranked = list(enumerate(events, 1))
        if sort == 'latest':
            ranked.sort(key=lambda entry: (entry[1].get('tier') == 'core', entry[1].get('latest_match', '')),
                        reverse=True)
        body += '<div id="results">' + "".join(render_card(e, i) for i, e in ranked) + '</div>'
        if data_absence:
            body += f'<div class="panel empty" role="status">{esc(data_absence)}</div>'
            if nearest := result.get('nearest_events'):
                body += render_article_list(nearest)
        elif not count and not no_context_match:
            message = ('주제와 충분히 가까운 기사가 없습니다(문장을 바꾸거나 필수 키워드를 넣어 보세요)'
                       if result['params'].get('mode') == 'topic' else
                       '일치하는 기사가 없습니다. 검색어를 바꿔 보세요.')
            body += f'<div class="panel empty">{message}</div>'
        excluded_articles = result.get('excluded_articles', [])
        other_articles = result.get('other_articles', [])
        if excluded_articles or other_articles:
            others = '국가대표' if result.get('national_team_sports') else ', '.join(result['params']['keywords'])
            others = f'다른 {others} 기사' if others else '다른 분야 기사'
            categories = f' ({esc(", ".join(result["excluded_categories"]))})' if not other_articles else ''
            articles = other_articles + excluded_articles
            body += (f'<details id="other-results" class="panel"><summary>{esc(others)} {len(articles):,}건{categories}</summary>'
                     f'{render_article_list(articles)}</details>')
    else:
        body = '<div class="panel empty">검색어를 입력해 사건을 찾아보세요.</div>'
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DICE · 뉴스 사건 검색</title><style>{STYLE}</style></head><body><main>
<header><div class="brand">DICE / NEWS SEARCH LAB</div><h1>뉴스를 사건으로 찾아보기</h1>
<p class="intro">온라인 기사 DB를 읽어, 같은 사건의 보도를 함께 보여주는 검색 목업입니다.</p></header>
<form class="panel" action="{action}" method="get"><div class="search-row">
<label for="ask">질문<input id="ask" name="ask" value="{value('ask')}" maxlength="500" placeholder="요즘 러브버그 많다던데"></label>
<input type="hidden" name="kw" value="{value('kw')}"><input type="hidden" name="q" value="{value('q')}">
<input type="hidden" name="date_from" value="{value('date_from')}"><input type="hidden" name="date_to" value="{value('date_to')}">
<input type="hidden" name="publisher" value="{value('publisher')}">
<button type="submit">검색</button></div><p class="hint">{esc(hint)}</p>{conversion_html}</form>
<details class="panel advanced-search"{' open' if advanced_open else ''}><summary><span class="advanced-title">세부 검색</span><span class="advanced-summary">{condition_summary}</span></summary>
<form action="{action}" method="get"><div class="search-row">
<label for="kw">필수 키워드<input id="kw" name="kw" value="{value('kw')}" maxlength="100" placeholder="강선우, 공천헌금"></label>
<label for="q">자연어 주제<input id="q" name="q" value="{value('q')}" maxlength="200" placeholder="재판 결과와 형량"></label>
<button type="submit">검색</button></div><div class="grid">
<label for="date_from">시작일<input type="date" id="date_from" name="date_from" value="{value('date_from')}"></label>
<label for="date_to">종료일<input type="date" id="date_to" name="date_to" value="{value('date_to')}"></label>
<label for="publisher">언론사<select id="publisher" name="publisher"><option value="">전체 언론사</option>{publisher_options}</select></label>
</div><p class="hint">해석한 조건을 직접 고쳐 검색하세요. 기간은 시작일과 종료일을 포함하며, 빈칸은 제한하지 않습니다.</p></form></details>
<details class="favorites"><summary>즐겨찾기 목록</summary><ul id="favorites-list"></ul>
<p id="favorites-empty" class="hint">저장한 검색이 없습니다.</p></details>
{body}<footer class="hint">기본 20개 사건 · 쉼표·공백으로 키워드 구분 · 같은 이름의 별칭은 하나만 일치해도 검색됩니다.</footer>
<script id="results-script">{RESULTS_SCRIPT}</script>
</main></body></html>'''


def handle_query(query: str, as_json: bool = False, timeline: bool = False, *,
                 page_path: str = '/', cover_months: bool = False, stats_only: bool = False) -> tuple[str | dict, int]:
    values = {'_page_path': page_path}

    def failure(message, status):
        return ({"error": message} if as_json else render_page(values, error=message)), status

    if len(query) > 16000:
        return failure("검색 조건이 너무 깁니다.", 400)
    try:
        parsed = parse_qs(query, max_num_fields=20, keep_blank_values=True)
        values.update({k: parsed.get(k, [""])[0] for k in ("q", "kw", "ask", "date_from", "date_to", "publisher")})
        values.update({key: parsed.get(key, [''])[0][:size] for key, size in RESULT_FILTERS.items()})
        if 'sort' in parsed:
            values['sort'] = 'similarity' if parsed['sort'] == ['similarity'] else 'latest'
        values['_resolved'] = parsed.get('resolved') == ['1']
        if len(values["q"]) > 200 or len(values["kw"]) > 100 or len(values['ask']) > 500:
            raise SearchError("q는 200자, kw는 전체 100자, 질문은 500자 이내로 입력하세요.")
        if not as_json:
            values['_ai_available'] = ollama_available()
            values['_publishers'] = list_publishers()
        mode = parsed.get("mode", [None])[0]
        if mode is not None and mode not in ("keyword", "topic"):
            raise SearchError("mode는 keyword 또는 topic이어야 합니다.")
        # 명시적인 kw가 없는 예전 keyword 링크의 q만 키워드로 옮긴다.
        if mode == "keyword" and "kw" not in parsed:
            values["kw"], values["q"] = values["q"], ""
        if not values['kw'].strip() and not values['q'].strip() and not values['ask'].strip():
            if not as_json:
                return render_page(values), 200
            result = search({})
            if stats_only:
                return {key: result[key] for key in ('stats', 'elapsed_ms', 'timings')}, 200
            if not timeline and parsed.get('stats') == ['0']:
                result.pop('stats', None)
            return result, 200
        params = {"keywords": values['kw'], "q": values['q']}
        params.update({k: values[k] for k in ('date_from', 'date_to', 'publisher') if values[k]})
        if values['ask'].strip() and not (values['_resolved'] and (values['kw'].strip() or values['q'].strip())):
            params['ask'] = values['ask']
        options = {'timeline': True} if timeline else {}
        if cover_months:
            options['cover_months'] = True
        if 'sort' in values:
            options['sort'] = values['sort']
        if as_json and not timeline and parsed.get('followups') == ['1']:
            options['include_followups'] = True
        if stats_only:
            options['stats_mode'] = 'only'
        elif as_json and not timeline and parsed.get('stats') == ['0']:
            options['stats_mode'] = 'deferred'
        term, name = (parsed.get(key, [''])[0] for key in ('web_term', 'web_name'))
        if (values['_resolved'] and 'ask' not in params and re.fullmatch(r'[가-힣A-Za-z0-9]{2,30}', term)
                and re.fullmatch(r'[가-힣A-Za-z0-9 ]{2,30}', name) and term in values['ask'] and name in values['q']):
            values['_web_alias'] = options['web_alias'] = {'term': term, 'canonical': name}
        if values['_resolved'] and values['ask'] and 'ask' not in params:
            partition = read_term_partition(parsed.get('term_partition', [''])[0])
            if partition is not None:
                values['_term_partition'] = options['term_partition'] = partition
        result = search(params, **options)
        if stats_only:
            return {key: result[key] for key in ('stats', 'elapsed_ms', 'timings', 'cache_hit') if key in result}, 200
        if 'sort' not in parsed and result.get('params', {}).get('mode') == 'digest':
            values['sort'] = 'similarity'
        if result.get('conversion'):
            values.update(_resolved=True, kw=', '.join(result['params']['keywords']), q=result['params']['q'],
                          _web_alias=result['conversion'].get('web_alias'),
                          _term_partition=result.get('term_partition'),
                          **{k: result['params'].get(k) or '' for k in ('date_from', 'date_to', 'publisher')})
        if as_json and values.get('sort') == 'latest' and not timeline:
            result = {**result, **{field: sorted(result[field], key=lambda e: (e.get('tier') == 'core', e['latest_match']),
                                               reverse=True)
                                   for field in ('events', 'related_events') if result.get(field)}}
        return (result if as_json else render_page(values, result=result)), 200
    except QuestionConversionError as exc:
        return failure(str(exc), 503)
    except ValueError:
        return failure("검색 조건 형식을 확인하세요.", 400)
    except SearchError as exc:
        return failure(str(exc), 400)
    except Exception:
        return failure("검색을 처리하지 못했습니다. 로컬 실행 환경을 확인하세요.", 500)


def read_term_partition(value: str) -> dict | None:
    if not value or len(value) > 2000:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get('term_source') not in ('model', 'legacy'):
        return None
    limits = {'required_terms': 60, 'supporting_terms': 8, 'person_names': 8}
    partition = {'term_source': parsed['term_source']}
    for key, limit in limits.items():
        words = parsed.get(key)
        if (not isinstance(words, list) or len(words) > limit
                or not all(isinstance(word, str) and re.fullmatch(r'[가-힣A-Za-z0-9 ]{1,100}', word)
                           and word.strip() == word for word in words)):
            return None
        partition[key] = list(dict.fromkeys(words))
    if set(partition['required_terms']).intersection(partition['supporting_terms']):
        return None
    if parsed.get('target_category') in MAJOR_CATEGORIES:
        partition['target_category'] = parsed['target_category']
    return partition


def timeline_json(generated: dict, params: dict, reports: dict, followups: dict, *, notice: str = '') -> dict:
    items = []
    by_month = {}
    for item in merge_timeline_items(generated['items']):
        start = str(item['date_from'])
        reports_for_item = timeline_reports(item, reports)
        later, _ = timeline_followups(item, followups)
        data = {key: value for key, value in item.items() if not key.startswith('_')}
        data['publishers'] = sorted(item['_publishers'])
        data.update(event_coverage(data))
        data.update(date_label=start if start == str(item['date_to']) else f"{start} ~ {item['date_to']}",
                    articles=reports_for_item, followups=later)
        items.append(data)
        by_month.setdefault(start[:7], []).append(data)
    summaries = {month['month']: month for month in generated.get('months', [])}
    months = []
    for month, events in sorted(by_month.items()):
        summary = summaries.get(month) or {
            'month': month, 'text': clean_month_title(timeline_month_representative(events)['representative']['title']),
            'source': '제목', 'checks': [],
        }
        months.append({**summary, 'event_count': len(events),
                       'article_count': sum(len(event['articles']) for event in events)})
    topic = params['q'] or ' · '.join(params['keywords']) or '주제'
    first, last = timeline_period(items, params.get('date_from'), params.get('date_to'))
    header = f'{topic} · {first[:7]} ~ {last[:7]}' if first and last else topic
    status = generated.get('summary_status', '생략')
    return {
        'header': header, 'headline': generated.get('headline', ''), 'months': months, 'items': items,
        'lead': generated.get('lead') if status in ('통과', '재생성 통과') else None,
        'summary_status': status, 'summary_checks': generated.get('summary_checks', []),
        'llm_ms': generated.get('llm_ms', 0), 'llm_calls': generated.get('llm_calls', 0),
        'generation_ms': generated.get('generation_ms', 0), 'model': generated.get('model', llm_model()),
        'cached': generated.get('cached', False), 'filter_relaxed': bool(items and generated.get('filter_relaxed')),
        'relevance_status': generated.get('relevance_status', '미실행'),
        'relevance_excluded_count': generated.get('relevance_excluded_count', 0),
        'notice': notice or ('질문과 관련된 사건을 찾지 못했습니다.' if not items else ''),
    }


def handle_publishers(_query: str) -> tuple[dict, int]:
    return {'publishers': [{'value': name, 'label': name} for name in major_publishers(list_publishers())]
                          + [{'value': OTHER_PUBLISHERS, 'label': '기타 언론사'}]}, 200


def handle_timeline(query: str, *, as_json: bool = False) -> tuple[str | dict, int]:
    result, status = handle_query(query, as_json=True, timeline=True, cover_months=as_json)
    if status != 200:
        return (result if as_json else f'<p class="hint">{esc(result["error"])}</p>'), status
    try:
        params = timeline_params(result)
        if as_json and params['mode'] == 'empty' and not result.get('ai_error'):
            return timeline_json({'items': []}, params, {}, {}), 200
        if message := result.get('data_absence_message') or DATA_ABSENCE_MESSAGES.get(result.get('data_absence')):
            if as_json:
                return timeline_json({'items': []}, params, {}, {}, notice=message), 200
            html = timeline_notice(message)
        elif result.get('ai_error') or (not params['q'] and not (result.get('events') or result.get('related_events')
                                                               or result.get('timeline_month_candidates'))
                                       and not ollama_available()):
            if as_json:
                return {'error': result.get('ai_error') or '로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다.'}, 503
            html = timeline_notice('로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다')
        else:
            generated = generate_timeline(result)
            if as_json and not generated['items'] and generated.get('relevance_status') == '판정 실패':
                return {'error': '관련성 판정을 완료하지 못했습니다. 잠시 후 다시 시도하세요.'}, 503
            reports, followups = timeline_articles(generated['items'], params)
            if as_json:
                return timeline_json(generated, params, reports, followups), 200
            html = render_timeline(generated, params['keywords'], reports, followups,
                                   context_label=timeline_context_label(params),
                                   topic_label=params['q'] if params.get('mode') != 'digest' else '',
                                   requested_period=(params.get('date_from'), params.get('date_to')))
    except LLMUnavailable:
        if as_json:
            return {'error': '로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다.'}, 503
        html = timeline_notice('로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다')
    except LLMError as exc:
        if as_json:
            return {'error': str(exc)}, 502
        html = f'<section class="panel"><p class="hint">{esc(exc)}</p></section>'
    except Exception:
        if as_json:
            return {'error': '타임라인을 정리하지 못했습니다. 잠시 후 다시 시도하세요.'}, 500
        html = '<p class="hint">타임라인을 정리하지 못했습니다. 잠시 후 다시 시도하세요.</p>'
    parsed = parse_qs(query, max_num_fields=20)
    if parsed.get('fragment') == ['1']:
        return html, 200
    page = '/classic' if frontend_file('/') else '/'
    return (f'<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>사건 타임라인</title><style>{STYLE}</style><main><a href="{page}?{esc(query)}">검색으로 돌아가기</a>'
            f'{html or "<p>로컬 AI 연결 안 됨 또는 정리할 사건이 없습니다. 수동 검색을 이용하세요.</p>"}</main></html>'), 200


API_HANDLERS = {
    '/api/search': lambda query: handle_query(query, as_json=True),
    '/api/stats': lambda query: handle_query(query, as_json=True, stats_only=True),
    '/api/timeline': lambda query: handle_timeline(query, as_json=True),
    '/api/publishers': handle_publishers,
}


import uvicorn
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

_duplicate_warmup_lock = Lock()
_duplicate_warmup_running = False


def start_duplicate_warmup() -> bool:
    global _duplicate_warmup_running
    with _duplicate_warmup_lock:
        if _duplicate_warmup_running:
            return False
        _duplicate_warmup_running = True
        def warmup():
            global _duplicate_warmup_running
            try:
                warm_duplicate_snapshot()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Duplicate cache warmup failed")
            finally:
                with _duplicate_warmup_lock:
                    _duplicate_warmup_running = False
        try:
            Thread(target=warmup, name="duplicate-cache-warmup", daemon=True).start()
        except Exception:
            _duplicate_warmup_running = False
            raise
        return True


@asynccontextmanager
async def lifespan(_app):
    start_model_warmup()
    start_duplicate_warmup()
    yield

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
if __package__:
    from .accounts import has_session as accounts_has_session, router as accounts_router
else:
    from accounts import has_session as accounts_has_session, router as accounts_router
app.include_router(accounts_router)

def login_required(path: str) -> bool:
    """REQUIRE_LOGIN=1이면 회원 API·warmup·정적 파일을 뺀 데이터 경로에 세션을 요구한다."""
    if os.environ.get("REQUIRE_LOGIN") != "1":
        return False
    if path.startswith("/api/"):
        return not path.startswith(("/api/auth/", "/api/favorites"))
    return path in ("/", "/classic", "/timeline")


def session_ok(request) -> bool:
    try:
        return accounts_has_session(request)
    except Exception:  # DB 장애 시 데이터는 막는다(fail closed).
        return False


@app.middleware("http")
async def protect(request: Request, call_next):
    if request.url.path == "/api/warmup":
        peer = request.client.host if request.client else ""
        if peer not in ("127.0.0.1", "::1") or any(
            header in request.headers for header in ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "origin")
        ):
            return JSONResponse({"error": "Not Found"}, status_code=404)
        return await call_next(request)
    if request.method in ('GET', 'HEAD') and (file := frontend_file(request.url.path)):
        return FileResponse(file, headers=FRONTEND_HEADERS, media_type=frontend_media_type(file))
    if login_required(request.url.path) and not await run_in_threadpool(session_ok, request):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401, headers=HEADERS)
        return HTMLResponse('<!doctype html><meta charset="utf-8"><p>로그인이 필요합니다. <a href="/">로그인 화면으로</a></p>',
                            status_code=401, headers=HEADERS)
    # 회원·즐겨찾기는 가벼운 요청이고 로그인 시도는 accounts.AttemptLimiter가 따로 제한한다.
    counted = not request.url.path.startswith(("/api/auth/", "/api/favorites"))
    status, message, extra = request_guard(request.headers, request.client.host if request.client else "unknown", counted)
    if status != 200:
        response = JSONResponse({"error": message}, status_code=status, headers=extra)
    else:
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"error": "요청을 처리하지 못했습니다."}, status_code=500)
    response.headers.update(HEADERS)
    return response

@app.get("/", response_class=HTMLResponse)
@app.get("/classic", response_class=HTMLResponse)
def index(request: Request):
    html, status = handle_query(request.url.query, page_path=request.url.path)
    return HTMLResponse(html, status_code=status, headers=HEADERS)

@app.post("/api/warmup", status_code=202)
def api_warmup():
    return {"started": start_duplicate_warmup()}


@app.get("/api/search")
@app.get("/api/stats")
@app.get("/api/timeline")
@app.get("/api/publishers")
def api(request: Request):
    data, status = API_HANDLERS[request.url.path](request.url.query)
    return JSONResponse(data, status_code=status, headers=HEADERS)

@app.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request):
    html, status = handle_timeline(request.url.query)
    return HTMLResponse(html, status_code=status, headers=HEADERS)

def _self_test() -> None:
    """통합 입력·이전 URL 호환·빈 입력·기사별 유사도·이스케이프를 DB 없이 점검한다."""
    from unittest.mock import patch

    html = render_page({"kw": '\"><script>alert(1)</script>', "q": "주제"})
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'class="tab' not in html and html.count('name="q"') == html.count('name="kw"') == 2
    assert html.count('<button') == 2 and ' required' not in html and 'name="ask"' in html
    assert 'name="pub"' not in html and 'name="from"' not in html
    stats = {"basis": '<script>집합</script>',
             "monthly": [{"month": "2026-01", "count": 0, "top_keywords": []},
                         {"month": "2026-02", "count": 2,
                          "top_keywords": [{"keyword": '<img onerror="x">', "mention_sum": 7,
                                            "article_count": 3, "lift": 1.56},
                                           {"keyword": 'R&D', "mention_sum": 7,
                                            "article_count": 3, "lift": 1.56}]}]}
    panel = render_stats(stats, 12.3)
    assert '<script>' not in panel and '&lt;script&gt;' in panel
    assert '<img' not in panel and '&lt;img' not in panel and 'R&amp;D' in panel
    assert panel.count('<section') == 1 and 'id="keyword-stats"' not in panel
    assert '월별 기사 수 · 주요 키워드' in panel and 'height="0.00"' in panel and 'height="125.00"' in panel
    assert '(7, ×1.6)</span>' in panel and '집계 12.3 ms' in panel
    assert '<table' in panel and '2026-01' in panel and 'viewBox="0 0 720 185"' in panel
    year = render_stats(dict(basis="12개월", monthly=[
        dict(month=f"2026-{i:02}", count=0, top_keywords=[]) for i in range(1, 13)]))
    assert year.count('<rect ') == 12 and '2026-12' in year and '12월' in year
    assert 'overflow-x:auto' not in STYLE and 'width:100%;height:auto' in STYLE
    empty = render_stats(dict(basis="빈 집합", monthly=[]))
    assert '집계할 기사가 없습니다.' in empty and 'nan' not in empty.lower()
    result = dict(events=[], stats=stats, timings={"stats_ms": 12.3}, candidate_count=0,
                  elapsed_ms=20, params={"mode": "keyword", "keywords": [], "q": ""})
    page = render_page({}, result)
    assert page.index('id="monthly-stats"') < page.index('class="results-head"')
    assert '최신순' in page
    topic_result = dict(result, params={"mode": "topic", "q": "주제", "keywords": []},
                        result_min_cos=.85, excluded_candidate_count=3)
    topic_page = render_page({}, topic_result)
    assert '기본 유사도 기준 0.85' in topic_page and '제외 3건' in topic_page
    assert '주제와 충분히 가까운 기사가 없습니다(문장을 바꾸거나 필수 키워드를 넣어 보세요)' in topic_page
    relaxed_page = render_page({}, dict(topic_result, result_filter_relaxed=True))
    assert '주제와 가까운 기사가 적어 필수 키워드 기준으로 보여 드립니다' in relaxed_page
    assert '기본 유사도 기준 0.85' not in relaxed_page and '제외 3건' not in relaxed_page
    assert f'로컬 AI({llm_model()})' in html
    assert title_link({"title": "<b>", "url": "javascript:alert(1)"}) == "&lt;b&gt;"
    assert 'href="https://example.com/?q=&quot;"' in title_link({"title": "기사", "url": 'https://example.com/?q="'})
    assert handle_query("mode=invalid")[1] == 400
    assert handle_query("mode=keyword&q=")[1] == 200
    assert handle_query("mode=topic")[1] == 200
    assert handle_query("", as_json=True)[0]["stats"] == {"basis": "검색 조건 없음", "monthly": []}
    assert handle_query("mode=invalid", as_json=True) == ({"error": "mode는 keyword 또는 topic이어야 합니다."}, 400)
    for key, length in (("q", 201), ("kw", 101)):
        assert handle_query(f"{key}=" + "x" * length)[1] == 400
        assert handle_query(f"{key}=" + "x" * length, as_json=True)[1] == 400
    with patch.dict(handle_query.__globals__, search=lambda _p: (_ for _ in ()).throw(
        RuntimeError("SELECT secret FROM private_table"))):
        for as_json in (False, True):
            payload, status = handle_query("kw=a", as_json=as_json)
            assert status == 500 and "SELECT" not in str(payload) and "private_table" not in str(payload)
    assert HEADERS["X-Frame-Options"] == "DENY"
    with patch.dict(os.environ, {}, clear=True), patch.dict(globals(), _requests={}):
        assert request_guard({}, "socket")[0] == 200
        # 인증 실패도 요청 한도에 포함하며 CF > XFF 첫 값 > 소켓 순서다.
        for _ in range(20):
            assert request_guard({"cf-connecting-ip": "cf", "x-forwarded-for": "xff, next"}, "socket")[0] == 200
        assert request_guard({"cf-connecting-ip": "cf"}, "other")[0] == 429
        assert request_guard({"cf-connecting-ip": "cf"}, "other", counted=False)[0] == 200
        assert request_guard({"x-forwarded-for": "xff, next"}, "socket")[0] == 200
        assert _requests["xff"][1] == 1 and _requests["socket"][1] == 1
        with patch.dict(request_guard.__globals__, monotonic=lambda: _requests["cf"][0] + 61):
            assert request_guard({"cf-connecting-ip": "cf"}, "socket")[0] == 200
    with patch.dict(os.environ, SEARCH_PASSWORD="자체점검"), patch.dict(globals(), _requests={}):
        for token in ("", "Basic !!!", "Basic " + base64.b64encode(b"no-colon").decode(),
                      "Bearer " + base64.b64encode("user:자체점검".encode()).decode(),
                      "Basic " + base64.b64encode(b"user:wrong").decode()):
            assert request_guard({"authorization": token}, "auth")[0] == 401
        token = base64.b64encode("아무나:자체점검".encode()).decode()
        assert request_guard({"authorization": "Basic " + token}, "auth")[0] == 200
    with patch.dict(handle_query.__globals__, search=lambda p: p), patch.dict(
        handle_query.__globals__, render_page=lambda _v, result=None, **_kw: result
    ):
        assert handle_query("mode=keyword&q=a,b")[0] == {"keywords": "a,b", "q": ""}
        assert handle_query("mode=topic&q=topic")[0] == {"keywords": "", "q": "topic"}
        assert handle_query("kw=a&q=topic")[0] == {"keywords": "a", "q": "topic"}
        assert handle_query("mode=keyword&kw=a&q=topic")[0] == {"keywords": "a", "q": "topic"}
        assert handle_query("kw=a&q=topic", as_json=True)[0] == {"keywords": "a", "q": "topic"}


def self_test() -> None:
    from unittest.mock import patch

    with patch.dict(globals(), ollama_available=lambda: True):
        _self_test()
        assert handle_query('ask=' + 'a' * 501)[1] == 400
        assert handle_timeline('q=' + 'a' * 201)[1] == 400
        empty = search({})
        with patch.dict(globals(), search=lambda _p: empty, ollama_available=lambda: False):
            page, status = handle_query('ask=질문')
            assert status == 200 and '로컬 AI 연결 안 됨' in page and 'timeline-slot' not in page
            assert '로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다' in handle_timeline('fragment=1')[0]
            assert '로컬 AI에 연결하지 못해 타임라인을 만들지 못했습니다' in page
        rep = dict(title='<script>', url='javascript:alert(1)', publisher='동아일보')
        article = dict(title='다른 보도', url='https://example.com', publisher='신문', published_date='2026-01-01')
        item = dict(group_id=1, date_from='2026-01-01', date_to='2026-01-01', date_label='2026-01-01',
                    title='<script>', summary='다른 보도', representative=rep,
                    article_count=2, publishers=['동아일보', '신문'], matches=[article, article])
        timeline = dict(headline='<script>', lead='다른 보도', model='qwen3:4b', llm_ms=1,
                        items=[item, dict(item), dict(item, group_id=2),
                               dict(item, group_id=3, date_from='2026-01-02', date_to='2026-01-02')])
        panel = render_timeline(timeline, ['강선우'])
        assert '<script>' not in panel and 'javascript:' not in panel
        assert panel.count('class="timeline-event"') == 2 and '<h2>강선우 · 2026.01 ~ 2026.01</h2>' in panel
        assert panel.count('동일 보도 보기') == 2 and panel.count('>다른 보도 ↗</a>') == 2
        assert '동아일보 외 1개 언론사' in panel
        assert '<h2>&lt;script&gt;</h2>' not in panel and '요약 검사: 생략' in panel
        assert 'timeline-lead' not in panel and 'class="timeline-ai-summary"' not in panel
        assert panel.count('class="flow-row"') == 1 and '(사건 2 · 보도 4건)' in panel
        assert 'class="timeline-flow"' in panel and '.flow-row{display:grid;' in STYLE
        failed_panel = render_timeline(dict(timeline, summary_status='생략',
                                           summary_checks=[{'reasons': ['길이 40~250자 위반']}]), ['강선우'])
        assert 'class="timeline-ai-summary"' not in failed_panel and 'AI 요약 보기' not in failed_panel
        summary_panel = render_timeline(dict(timeline, lead='강선우 <script> 흐름 요약', summary_status='재생성 통과'), ['강선우'])
        assert '강선우 &lt;script&gt; 흐름 요약' in summary_panel and '<script>' not in summary_panel
        assert '<details class="timeline-ai-summary"><summary>AI 요약 보기 (qwen3:4b · 기사 제목·제공된 본문 요약 근거 · 원문 확인 필요)' in summary_panel
        assert '<details class="timeline-ai-summary" open' not in summary_panel
        assert '요약 검사: 재생성 통과' in summary_panel
        flow_items = [dict(item, group_id=i, date_from=day, date_to=day,
                           representative=dict(rep, title=title))
                      for i, (day, title) in enumerate([
                          ('2026-01-01', '경찰 수사 본격화'),
                          ('2026-01-15', '구속영장 신청'),
                          ('2026-01-20', '검찰 기소 결정'),
                          ('2027-02-01', '징역 2년 선고')])]
        flow_items[0]['article_count'], flow_items[1]['article_count'], flow_items[2]['article_count'] = 2, 8, 5
        month_inputs = timeline_month_input(flow_items)
        months = match_timeline_months({'months': [
            {'month': '2026-01', 'steps': ['경찰 수사 착수', '구속영장 신청', '검찰 기소 결정']}]}, month_inputs)['months']
        flow_panel = render_timeline(dict(timeline, items=flow_items, months=months), ['강선우'])
        flow = flow_panel.split('<div class="timeline-flow">', 1)[1].split('</ul><p class="hint flow-notice">', 1)[0]
        assert '초 경찰 수사 착수 → 1/15 구속영장 신청 → 1/20 검찰 기소 결정' in flow
        assert '구속영장 신청 · 검찰 기소 결정' not in flow
        assert '(사건 3 · 보도 15건)' in flow and '2027년 2월' in flow
        assert flow.count('class="flow-row"') == 2 and '2026.01 ~ 2027.02</h2>' in flow_panel
        assert '기사 제목·제공된 요약 근거 · 원문 확인 필요' in flow_panel and '>흐름 요약</small>' in flow
        fallback_item = dict(item, representative=dict(rep, title='[속보] “강선우” 공천헌금 구속영장 신청'))
        fallback_panel = render_timeline(dict(timeline, items=[fallback_item], summary_status='생략'),
                                         ['강선우', '공천헌금'])
        fallback_flow = fallback_panel.split('<div class="timeline-flow">', 1)[1].split('</div>', 1)[0]
        assert '강선우 공천헌금 구속영장 신청' in fallback_flow and '>대표 제목</small>' in fallback_flow
        assert '…' not in fallback_flow and 'title="대표 사건의 제목' in fallback_flow
        assert '전체 LLM' in fallback_panel
        ten_items = [dict(item, group_id=i, date_from=f'2026-{i:02}-01', date_to=f'2026-{i:02}-01',
                          representative=dict(rep, title='강선우 1심 판결')) for i in range(1, 11)]
        ten_panel = render_timeline(dict(timeline, items=ten_items), ['강선우'])
        assert ten_panel.count('<details class="flow-month-details">') == 10
        assert ten_panel.count('class="timeline-event"') == 10 and '펼쳐서 더보기' not in ten_panel
        assert '>4월</strong>' in ten_panel and '2026-04-01' in ten_panel
        escaped = render_timeline(dict(timeline, months=[dict(months[0], text='<script>', source='AI')]), ['강선우'])
        assert '<script>' not in escaped and '&lt;script&gt;' in escaped
        assert '.flow-count{grid-column:2;white-space:normal}' in STYLE
        assert '관련 사건이 부족해 타임라인을 만들지 못했습니다(필수 키워드를 줄이거나 바꿔 보세요)' in render_timeline(dict(timeline, items=[]), [])
        assert '주제와 가까운 사건이 적어 유사도 기준을 완화했습니다' in render_timeline(dict(timeline, filter_relaxed=True), [])
        judged = dict(timeline, relevance_status='판정 완료', relevance_excluded_count=3)
        assert '관련성 판정으로 3건 제외' in render_timeline(judged, [])
        zero = dict(judged, items=[], no_relevant_events=True)
        with patch.dict(globals(), generate_timeline=lambda _r: zero, ollama_available=lambda: False,
                        handle_query=lambda *_a, **_kw: ({'params': {'keywords': ['강선우'], 'q': '주제'}}, 200)):
            notice, status = handle_timeline('fragment=1')
        assert status == 200 and '질문과 관련된 사건을 찾지 못했습니다' in notice
        assert '관련성 판정으로 3건 제외' in notice
        assert similar_title('강선우, 공천헌금!', '강선우 공천헌금')
        assert similar_title('강선우 공천헌금 사건 수사', '강선우 공천헌금 사건')
        assert similar_title('강선우 공천헌금 사건 경찰 수사 본격화', '강선우 공천헌금 사건 검찰 수사 본격화')
        assert not similar_title('서로 다른 사건', '전혀 별개의 보도')
        chain = [dict(item, group_id=1), dict(item, group_id=2, date_from='2026-01-02'),
                 dict(item, group_id=2)]
        assert len(merge_timeline_items(chain)) == 1
        sub = dict(group_id=1, date_from='2026-01-01', date_to='2026-01-02', representative=rep)
        merged = dict(item, event_count=2, article_count=7,
                      sub_events=[sub, dict(sub, group_id=2, representative=article)])
        merged_panel = render_timeline(dict(timeline, items=[merged]), ['강선우'])
        assert '관련 보도 2건 · 사건 2개' in merged_panel and '후속 기사 보기' in merged_panel
        assert f'종료 후 {FOLLOWUP_WINDOW_DAYS}일' in merged_panel and '>다른 보도 ↗</a>' in merged_panel
        # 엔진이 분리한 사건을 제목·날짜가 같다는 이유로 다시 합치지 않는다.
        assert render_timeline(dict(timeline, items=[merged, dict(merged, group_id=3)]), []).count('class="timeline-event"') == 2
        more_items = [dict(item, group_id=i, date_from=f'2026-01-0{i}', date_to=f'2026-01-0{i}')
                      for i in range(1, 5)]
        single_month = render_timeline(dict(timeline, items=more_items), ['강선우'])
        assert single_month.count('<details class="flow-month-details">') == 1
        assert single_month.count('class="timeline-event"') == 4 and '펼쳐서 더보기' not in single_month
        stub = dict(events=[{}], params=dict(keywords=['강선우'], q='관세 " </script>'))
        placeholder = timeline_placeholder(stub)
        assert '<script>' + TIMELINE_SCRIPT + '</script>' in placeholder
        assert '<noscript>' in placeholder and 'credentials:' in TIMELINE_SCRIPT
        assert 'hidden' not in TIMELINE_SCRIPT and '관련 사건이 부족해' in TIMELINE_SCRIPT
        assert '로컬 AI가 정리하고 있습니다 (보통 5~25초)' in placeholder
        assert _script_hash == base64.b64encode(hashlib.sha256(TIMELINE_SCRIPT.encode()).digest()).decode()
        assert 'connect-src \'self\'' in HEADERS['Content-Security-Policy']
        assert f"sha256-{_script_hash}" in HEADERS['Content-Security-Policy']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=os.environ.get("SEARCH_PORT", PORT),
                        help="로컬 서버 포트 (SEARCH_PORT, 기본 8010)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("포트는 1~65535 사이여야 합니다.")
    print(f"로컬 뉴스 검색: http://{HOST}:{args.port}/", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
