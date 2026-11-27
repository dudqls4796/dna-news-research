# 네이버 뉴스 CSV 수집

`node n8n/test_preprocess.js`로 합성 자료를 검사하고, `node n8n/build_workflow.js`로 가져오기용 JSON을 생성한다. `news_common.js`는 빌드 시 각 Code 노드 앞에 삽입하므로 공통 코드 변경 후 반드시 다시 생성한다. 로컬 n8n이나 Credential은 이 스크립트들로 변경하지 않는다.

흐름: 검색 → 원문 대상 만들기 → 기사별 순회(batchSize=1) → 원문 분석 → 순회 완료 → 전처리·CSV 만들기 → CSV 저장. 기존 일정(0·9·13·18시 KST), Credential 참조 `ptRshOfVJHFFbtFZ`, CSV 5종과 저장 경로는 유지한다.

v0.1.74부터 제목은 `articles.title`에 한 번만 저장한다. CSV 컬럼 순서는 `articles`: `id,source_id,published_date,title,loaded_at,url`, `article_categories`: `article_id,level,major,minor`이다. 나머지 3종의 컬럼은 같다. 새 DB에는 `백엔드/db/schema.sql`을 적용하고, 기존 DB에는 `백엔드/db/migrations/001_move_title_to_articles.sql`을 적용한 뒤 새 JSON을 가져온다. 이전 CSV는 새 컬럼으로 변환해야 하며, COPY 시 컬럼 목록을 명시한다(이전 DB와 신규 DB는 title의 물리적 컬럼 순서가 다를 수 있다).

원문 HTTP 요청을 별도 HTTP Request 노드로 분리하면 HTML이 실행 이력에 저장될 수 있다. 원문 요청·binary 버퍼·문자셋 디코딩·본문 추출·집계를 **원문 분석 Code 노드 메모리 안에서만** 수행한다. 반환값은 기사 메타데이터, 키워드별 횟수, 기자명, 고정 상태 코드뿐이다. 원문/HTML/응답/예외는 CSV·파일·로그·노드 출력·pinData·staticData에 넣지 않는다. staticData에는 실행 ID, robots 규칙, 요청 시각, 요청 건수만 둔다. 테스트는 외부 기사를 열지 않고 합성 HTML과 가상 HTTP만 사용하며 결과 요약만 출력한다.

실제 실행 전 외부 JS runner에서 **`NODE_FUNCTION_ALLOW_BUILTIN=http,https,url`**을 허용해야 한다. 기존 허용 목록이 있으면 합친다. 외부 npm 모듈은 필요 없다. n8n 1.111.0 runner는 `TextDecoder`를 제공하며 Node 22의 ICU EUC-KR 디코더를 사용한다. 모듈 미허용은 명확한 설정 오류로 중단한다. 이번 구현은 설정/컨테이너/Credential을 수정하지 않으므로 Claude가 가져오기와 실제 실행 시 확인해야 한다. [n8n 공식 모듈 설정 문서](https://docs.n8n.io/hosting/configuration/configuration-examples/modules-in-code-node/), [v1.111.0 runner 소스](https://github.com/n8n-io/n8n/blob/n8n%401.111.0/packages/%40n8n/task-runner/src/js-task-runner/js-task-runner.ts).

- 원문 상한: `news_common.js`의 `MAX_PAGES_PER_RUN=1000`. robots 요청을 제외한 원문 HTTP 시도 수이며 리다이렉트도 포함한다. 상한 밖 기사도 CSV에는 남기고 제목 키워드와 빈 기자명을 쓴다.
- 도메인(origin)별 robots를 실행마다 확인하고 캐시한다. 특정 User-Agent 우선, 최장 경로 일치, 동률 Allow 우선, `*`/`$`, Crawl-delay/Request-rate를 지원한다. 404/410은 규칙 없음, 조회 실패·HTML 응답·거부는 원문 수집 중단이다. 원문 리다이렉트 목적지도 검사한다.
- 요청 시작 간격은 전체 최소 500ms, 출처별로 더 긴 robots 간격을 적용한다. 한 번의 대기가 60초를 넘으면 해당 기사를 건너뛴다. 요청당 전체 제한 10초, 응답 2MiB, 리다이렉트 최대 5회. 서버 압축 응답은 identity 요청을 무시한 것으로 보고 제목 집계로 대체한다.
- Content-Type charset을 우선하고 없으면 meta charset/http-equiv를 확인한다. UTF-8 기본값이며 EUC-KR/CP949는 TextDecoder로 디코딩한다. 디코딩 실패, 유료/JS 전용/짧은 본문 등은 제목 집계와 빈 기자명으로 대체한다.
- 본문은 기존 crawler의 컨테이너, articleBody 계열, `<article>`, 문단 밀도 순으로 선택한다. 단순 HTML 스캐너이므로 모든 언론사의 복잡한 마크업을 보장하지 않는다. 기자명은 author 메타 → 기사 JSON-LD author → `이름 기자/특파원` 서명 순으로 찾는다. 각 후보를 공백 없는 한글 2~4자로 제한하고, 현재/알려진 언론사명(부분 이름 포함), og:site_name, 기관·일반어·용언/복합 조사 형태를 제외한다. 무효 후보만 있으면 다음 출처를 확인하고, 복수 이름은 쉼표로 연결한다. 외국어·5자 이상 이름 및 제외 규칙과 겹치는 실제 이름도 빈칸이 될 수 있다.
- 제목+정제 본문 전체를 조사·불용어 규칙으로 집계한다. named/numeric HTML entity를 디코딩하고 미지원 named entity는 공백 처리한다. 기사 기능어·보도 상용어와 짧은 용언은 조사 제거 전후에 제외하며 북한·권한 같은 명사는 예외로 보존한다. 기자명·이메일·URL·저작권 안내를 제거하고 상위 30개를 횟수 내림차순, 동률은 첫 출현 순서로 남긴다. 본문 추출 실패는 제목만 집계한다. 분류와 Jaccard 그룹은 제목만 사용하므로 원문 길이·수집 성공 여부가 그룹을 흔들지 않는다.
- 기자명 제외 어휘는 공통 분류 사전(`CATEGORIES`의 대·소분류/판별 단어, `KEYWORD_MAJOR`)과 섹션·메뉴 단어를 재사용한다. 후보 전체가 일치할 때만 제외해 혼합 목록의 유효 기자명과 이름의 부분 문자열을 보존한다. 이 목록은 키워드 불용어에 추가하지 않으므로 중국·시민 같은 본문 주제어는 계속 집계한다.
- 검색 응답은 n8n `itemMatching`으로 검색 요청에 연결하며, 이후 기사 ID·URL·수집 구간을 계속 전달한다. 순회 중 슬롯/날짜가 바뀌어도 파일명·ID·적재 시각이 달라지지 않는다. 검색 결과가 비어도 CSV 헤더 5개를 생성한다.

1,000건은 정상 응답만 있어도 최소 약 8분 이상 걸릴 수 있다. 기사마다 별도 runner 작업으로 실행해 단일 Code 작업의 장시간 실행을 피한다. 실제 가져오기·스케줄·브라우저(n8n 실행) 검증은 미완료다.
