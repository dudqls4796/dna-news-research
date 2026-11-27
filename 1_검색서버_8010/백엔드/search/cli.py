"""실행: python 백엔드/search/cli.py --keywords '강선우,공천헌금' --q '재판 결과와 형량'."""

import argparse
import json
import sys

try:
    from .engine import SearchError, LLMError, search, self_test, generate_timeline
except ImportError:
    from engine import SearchError, LLMError, search, self_test, generate_timeline


def format_result(result: dict) -> str:
    lines = [f"검색: 필수 키워드 {', '.join(result['params']['keywords']) or '없음'} · 주제 {result['params']['q'] or '없음'}",
             f"사건 {len(result['events'])}건 · 후보 {result['candidate_count']}건 · {result['elapsed_ms']:.1f}ms",
             "※ 관련 기사 수·기간은 전체 묶음 기준, 매칭 기사는 검색 조건을 통과한 후보입니다."]
    if result['params']['mode'] == 'combined':
        cap = f"후보 상한 {result['candidate_limit']}건 적용" if result['candidate_limit_applied'] else "후보 상한 미적용"
        lines.append(f"키워드 교집합 {result['keyword_candidate_count']}건 · 정확 거리 평가 {result['ranked_candidate_count']}건 · {cap}")
    stats = result['stats']
    lines.extend([f"통계: {stats['basis']} · {sum(m['count'] for m in stats['monthly'])}기사 · "
                  f"{result['timings'].get('stats_ms', 0):.1f}ms", "월별 기사 수 · 주요 키워드", "월 | 기사 수 | 주요 키워드"])
    for month in stats['monthly']:
        keywords = " · ".join(f"{k['keyword']}({k['mention_sum']:,}, x{k['lift']:.1f})"
                              for k in month['top_keywords']) or '—'
        lines.append(f"{month['month']} | {month['count']:,} | {keywords}")
    for i, event in enumerate(result["events"], 1):
        rep = event["representative"]
        ranking = f" (유사도 {event['score']:.4f})" if event['score'] is not None else ""
        lines.extend([f"\n{i}. {rep['title']}{ranking}",
                      f"   {rep['publisher'] or '언론사 미상'} · {rep['published_date']}",
                      f"   {rep['url'] or 'URL 없음'}",
                      f"   관련 기사 {event['article_count']}건 · {event['date_from']} ~ {event['date_to']}",
                      f"   분류: {', '.join(rep['categories']) or '없음'}"])
        for match in event["matches"]:
            similarity = f" | 유사도 {match['score']:.4f}" if match['score'] is not None else ""
            lines.append(f"   - {match['title']} | {match['publisher'] or '언론사 미상'} | {match['published_date']}{similarity}")
    if not result["events"]:
        lines.append("일치하는 기사가 없습니다. 검색어를 바꿔 보세요.")
    if result.get("conversion"):
        lines.append("질문 변환: " + json.dumps(result["conversion"], ensure_ascii=False))
    if result.get("ai_error"):
        lines.append(result["ai_error"])
    if result.get("timeline"):
        lines.append("사건 타임라인 · 날짜순\n" + json.dumps(result["timeline"], ensure_ascii=False))
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="JSON 객체. 생략 시 stdin")
    parser.add_argument("--mode", choices=("keyword", "topic"), help="이전 호출 호환용")
    parser.add_argument("--keywords", "--kw", help="쉼표·공백 구분 필수 키워드 (모두 일치)")
    parser.add_argument("--q", help="자연어 주제 문장")
    parser.add_argument("--ask", help="로컬 AI로 검색 조건을 만들 질문 (최대 500자)")
    parser.add_argument("--timeline", action="store_true", help="검색 뒤 로컬 AI 사건 타임라인 생성")
    parser.add_argument("--json", action="store_true", help="JSON 결과 출력")
    parser.add_argument("--self-test", action="store_true", help="DB 없는 검색·통계 계산식·패널·HTML 점검")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            try:
                from .app import self_test as page_self_test
            except ImportError:
                from app import self_test as page_self_test
            page_self_test()
            empty = format_result(search({}))
            assert "월 | 기사 수 | 주요 키워드" in empty and "상위 20" not in empty
            print("자체 점검 통과 (DB 접속 없음)")
            return 0
        if args.q is not None or args.keywords is not None or args.ask is not None:
            if args.query is not None:
                parser.error("JSON 인자와 --keywords/--q/--ask를 함께 사용할 수 없습니다.")
            params = {"q": args.q or ""}
            if args.keywords is not None:
                params["keywords"] = args.keywords
            if args.mode is not None:
                params["mode"] = args.mode
            if args.ask is not None:
                params["ask"] = args.ask
        else:
            if args.mode is not None:
                parser.error("--mode는 --q와 함께 사용하세요. JSON에는 mode를 포함하세요.")
            params = json.loads(args.query if args.query is not None else sys.stdin.read())
        result = search(params)
        if args.timeline and not result.get("ai_error"):
            result["timeline"] = generate_timeline(result)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_result(result))
        return 0
    except json.JSONDecodeError:
        message = "올바른 JSON 검색 조건을 입력하세요."
    except (SearchError, LLMError) as exc:
        message = str(exc)
    print(json.dumps({"error": message}, ensure_ascii=False) if args.json else message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
