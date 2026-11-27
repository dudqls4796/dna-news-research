"""동아일보 본문은 메모리에서만 요약한다. 기본/최대 20묶음, 기존 요약은 보존한다.

실행: python 백엔드/db/summarize_groups.py --limit 20
스키마 적용은 migrations/002_article_groups_summary.sql을 별도로 실행한다.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '백엔드'))
from crawler.crawl_news import fetch, new_http_state, parse_article  # noqa: E402
from search import engine, prompts  # noqa: E402

MAX_BYTES = 2 * 1024 * 1024
ARTICLE_DEADLINE = 40
SUMMARY_SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
                  'required': ['summary'], 'additionalProperties': False}


def donga_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme in ('http', 'https') and parts.hostname in ('www.donga.com', 'donga.com')
                and parts.port in (None, 80, 443) and not parts.username and not parts.password)
    except (ValueError, TypeError):
        return False


class DongaSession(requests.Session):
    """크롤러의 모든 요청(robots/리다이렉트 포함)에 도메인·용량·시간 상한을 추가한다."""

    def __init__(self):
        super().__init__()
        self.deadline = 0.0

    def get(self, url, **kwargs):
        if not donga_url(url):
            raise ValueError('동아일보 외 URL 차단')
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ValueError('기사 처리 시간 초과')
        kwargs.update(timeout=min(10, remaining), stream=True, allow_redirects=False)
        response = super().get(url, **kwargs)
        try:
            chunks, size = [], 0
            if response.status_code == 200:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES or monotonic() >= self.deadline:
                        raise ValueError('응답 용량 또는 시간 초과')
                    chunks.append(chunk)
            response._content = b''.join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()


class TrialState(dict):
    def __setitem__(self, key, value):
        if key == 'gap' and value > 20:
            # 다음 기사에서도 robots 간격을 잊지 않도록 실제 값을 보존한 뒤 중단한다.
            super().__setitem__(key, value)
            raise ValueError('robots/Retry-After 간격이 시험 상한을 초과함')
        super().__setitem__(key, value)


def validate_body_summary(summary, body: str) -> bool:
    """숫자·단위(월 포함)와 날짜가 입력 본문에 존재하는지 보수적으로 대조한다."""
    if not isinstance(summary, str) or not 20 <= len(summary) <= 1500:
        return False
    sentences = re.findall(r'[^.!?]+[.!?](?:\s|$)', summary)
    if not 2 <= len(sentences) <= 3 or not summary.rstrip().endswith(('.', '!', '?')):
        return False
    if engine.month_number_tokens(summary) - engine.month_number_tokens(body):
        return False
    # 숫자 일부의 우연한 일치(20 vs 2026)와 연월일 조합의 날조도 막는다.
    def numbers(text):
        return set(re.findall(r'(?<!\d)\d+(?:[.,]\d+)*(?!\d)', text))
    if numbers(summary) - numbers(body):
        return False
    dates = re.findall(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일', summary)
    return all(re.sub(r'\s', '', value) in re.sub(r'\s', '', body) for value in dates)


def summarize(url: str, state: dict) -> tuple[str | None, str]:
    if not donga_url(url):
        return None, '동아일보 URL 없음'
    endpoint = urlsplit(os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434'))
    if endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return None, '로컬 LLM 주소 필요'
    try:
        if state['gap'] > 20:
            return None, '요청 간격 초과'
        state['session'].deadline = monotonic() + ARTICLE_DEADLINE
        response = fetch(url, state)
        article = parse_article(response.content, response.url, '동아일보')
        body = article['content'][:12000]
        # 본문과 HTML, 원시 응답, 원시 예외는 파일/로그/DB에 기록하지 않는다.
        raw, _ = engine.ollama_chat(prompts.ARTICLE_SUMMARY, {'title': article['title'], 'body': body}, SUMMARY_SCHEMA)
        summary = raw.get('summary')
        if not validate_body_summary(summary, body):
            return None, '요약 검사 실패'
        return summary.strip(), '성공'
    except Exception:
        return None, '원문 읽기 또는 요약 실패'


def run(limit: int = 20, *, donga_only: bool = False) -> dict:
    if not 1 <= limit <= 20:
        raise ValueError('시험 실행은 1~20개 묶음만 허용')
    state = TrialState(new_http_state())
    headers = dict(state['session'].headers)
    state['session'].close()
    state['session'] = DongaSession()
    state['session'].headers.update(headers)
    results, reasons = [], Counter()
    try:
        with psycopg.connect(**engine.connection_options(Path(os.environ.get(
                'BIGKINDS_ENV_FILE', str(engine.DEFAULT_ENV)))), autocommit=True,
                row_factory=dict_row, connect_timeout=10, application_name='dice_summary_trial') as conn:
            conn.execute("SET statement_timeout='15s'; SET lock_timeout='5s'")
            selection = ("AND EXISTS (SELECT 1 FROM article_groups dg JOIN article_sources ds "
                         "ON ds.article_id=dg.article_id WHERE dg.group_id=article_groups.group_id "
                         "AND ds.publisher='동아일보')" if donga_only else '')
            rows = conn.execute(f'''
                SELECT chosen.group_id, target.article_id, target.url
                FROM (SELECT group_id FROM article_groups GROUP BY group_id
                      HAVING count(summary)=0 {selection} ORDER BY group_id DESC LIMIT %s) chosen
                LEFT JOIN LATERAL (
                    SELECT g.article_id, a.url FROM article_groups g
                    JOIN articles a ON a.id=g.article_id
                    JOIN article_sources s ON s.article_id=a.id
                    WHERE g.group_id=chosen.group_id AND s.publisher='동아일보'
                    ORDER BY g.is_representative DESC, a.published_date, a.id LIMIT 1
                ) target ON TRUE
            ''', (limit,)).fetchall()
            for row in rows:
                summary, reason = summarize(row['url'], state) if row['url'] else (None, '동아일보 기사 없음')
                saved = False
                if summary:
                    saved = conn.execute('''UPDATE article_groups SET summary=%s
                        WHERE article_id=%s AND group_id=%s AND summary IS NULL
                        AND NOT EXISTS (SELECT 1 FROM article_groups WHERE group_id=%s AND summary IS NOT NULL)
                        RETURNING article_id''', (summary, row['article_id'], row['group_id'], row['group_id'])).fetchone() is not None
                    if not saved:
                        reason = '기존 요약 보존'
                reasons[reason] += 1
                results.append({'group_id': row['group_id'], 'success': saved, 'reason': reason})
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    finally:
        state['session'].close()
    report = {'total': len(results), 'success': sum(r['success'] for r in results),
              'null': sum(not r['success'] for r in results), 'reasons': dict(reasons)}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=20, choices=range(1, 21))
    parser.add_argument('--donga-only', action='store_true', help='동아일보가 포함된 묶음만 시험')
    args = parser.parse_args()
    try:
        run(args.limit, donga_only=args.donga_only)
    except Exception:
        print('요약 시험 실행 실패: DB 연결·마이그레이션·의존성을 확인하세요.', file=sys.stderr)
        sys.exit(1)
