# 회원·즐겨찾기 API v0.2.174

같은 출처의 `/api`를 사용하며 시간은 UTC ISO 8601이다. 즐겨찾기에는 회원 세션이 필요하다. 검색은 `REQUIRE_LOGIN=1` 환경에서 회원 세션을 확인한다.

## DB 적용 (2026-11-26 적용 완료)

1. 기존 DB에 `../db/migrations/003_users_favorites.sql`을 적용했다. 새 DB는 전체 `../db/schema.sql`만 사용하며 둘을 중복 실행하지 않는다.
2. 학원 DB 계정에 역할 생성 권한이 없어 전용 DB 역할은 만들지 않는다(사용자 결정). 회원 API는 검색과 같은 접속 설정(`백엔드/.env` 또는 `BIGKINDS_DSN`)으로 **쓰기 가능한 별도 연결**을 연다. 검색 경로의 연결은 계속 읽기 전용이다.

회원 연결은 트랜잭션 단위로 commit/rollback한다. 접속 설정이 없으면 회원 API만 503이다. 회원 테이블 4개만 쓰며 기사 정보는 `articles(id,title,url,published_date)`와 `article_sources(article_id,publisher)`만 조회한다. 본문은 받거나 저장하지 않는다. 회원·즐겨찾기 요청은 검색 요청 한도(IP당 분당 20건)에서 제외하고, 가입·로그인·탈퇴 시도는 IP당 분당 10회로 따로 제한한다.

## 쿠키·CSRF·기존 공유 인증

- 모든 POST·PATCH·DELETE에 `X-DICE-CSRF: 1`이 필수다. JSON 본문에는 `Content-Type: application/json`도 보낸다. 커스텀 헤더와 CORS 미허용으로 교차 출처 form/preflight를 막는다. 향후 임의 출처 CORS를 허용하면 이 방어를 재검토해야 한다.
- 로그인은 `dice_session` 쿠키를 설정한다: `HttpOnly; SameSite=Lax; Path=/; Max-Age=1209600`(14일), Domain 미설정. 서버에는 SHA-256 토큰 해시만 저장한다. JS에서 토큰을 읽거나 localStorage에 저장하지 않는다. 만료는 자동 연장하지 않는다. 재로그인은 현재 쿠키의 세션을 교체하고 다른 기기의 세션은 유지한다. 탈퇴는 모든 세션을 삭제한다.
- HTTPS 요청에 `Secure`를 붙인다. TLS 종료 터널은 ASGI 서버가 **신뢰한 프록시만** 전달 스킴을 반영하게 설정한다. 대안으로 `AUTH_TRUSTED_PROXY_IPS`에 실제 직전 프록시 IP를 쉼표로 지정하면 그 peer의 `X-Forwarded-Proto: https`만 신뢰한다(기본 빈 목록, `*` 미지원). Uvicorn의 proxy-headers/forwarded-allow-ips 신뢰 설정에도 같은 원칙이 적용된다. 외부 클라이언트가 프록시로 가장할 수 없게 구성한다.
- `fetch`는 `credentials:'same-origin'`을 사용한다. 개발 Vite도 `/api` 프록시를 사용한다. 터널 호스트가 바뀌면 다시 로그인한다.
- `SEARCH_PASSWORD`가 설정된 환경은 기존 HTTP Basic 인증도 필요하다. 회원 쿠키는 공유 접근 제한을 대체하지 않는다. 기존 전역 60초 20요청 제한과 보안 헤더·`Cache-Control: no-store`를 유지한다.
- 가입·로그인·탈퇴 재확인은 별도 IP별 60초 10회 제한(성공 포함), 초과 429/`Retry-After: 60`. `ponytail`: 메모리·프로세스별이라 재시작/여러 워커/분산 IP에 공유되지 않는다. 프록시 설정에 따라 여러 사용자가 같은 한도를 공유한다. 운영 규모가 커지면 공유 저장소 기반 제한이 필요하다.

```js
async function memberApi(path, method = 'GET', body) {
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: {
      ...(method === 'GET' ? {} : {'X-DICE-CSRF': '1'}),
      ...(body === undefined ? {} : {'Content-Type': 'application/json'}),
      // SEARCH_PASSWORD 환경은 기존 Basic Authorization 처리도 유지한다.
    },
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error ?? '요청 실패')
  return data
}
```

## 회원

아이디는 ASCII 영문·숫자·`_` 4~20자이며 대문자는 소문자로 정규화한다. 아이디 공백은 거절한다. 비밀번호는 8~256자이고 공백/대소문자를 그대로 사용한다. 이메일·이름·비밀번호 확인 등 추가 필드는 보내지 않는다. 비밀번호 확인 일치는 프론트가 확인한다.

| 메서드/경로 | 요청 JSON | 성공 |
|---|---|---|
| POST `/api/auth/signup` | `{"login_id":"dice_user","password":"example-pass-123"}` | 201 `{"user":{...}}` |
| POST `/api/auth/login` | 가입과 같은 구조 | 200 `{"user":{...}}` + 쿠키 |
| GET `/api/auth/me` | 없음 | 200 `{"user":{...}}` |
| POST `/api/auth/logout` | 없음 | 200 `{"ok":true}` + 쿠키 제거 |
| DELETE `/api/auth/me` | `{"password":"example-pass-123"}` | 200 `{"ok":true}` + 쿠키 제거 |

공개 사용자 응답 예시:

```json
{"user":{"id":1,"login_id":"dice_user","created_at":"2026-11-26T00:00:00Z","last_login_at":"2026-11-26T00:01:00Z"}}
```

가입은 기본 폴더를 만들고 **자동 로그인하지 않는다**. 가입 응답의 `last_login_at`은 null이다. 가입 후 로그인 API를 호출한다. 로그인 실패는 없는 아이디/잘못된 비밀번호 모두 같은 401이며 없는 아이디도 더미 scrypt를 비교한다. DB 조회·잠금까지 완전히 같은 시간을 보장하지는 않는다. 로그아웃은 없는/만료 세션도 성공한다.

탈퇴는 현재 세션과 현재 비밀번호를 확인한다. 한 트랜잭션에서 계정과 FK CASCADE로 모든 세션·폴더·즐겨찾기를 즉시 삭제한다. 공용 기사와 다른 회원 데이터는 유지한다.

## 폴더

이름은 공백 정리 후 1~100자, 회원별 유일하다. `query`는 최대 500자/null이며 생략 기본값은 null이다. 폴더의 구분 키는 서버 ID다.

| 메서드/경로 | 요청 | 성공 |
|---|---|---|
| GET `/api/favorites/folders` | 없음 | 200 `{"folders":[{...}]}` |
| POST `/api/favorites/folders` | `{"name":"경제 뉴스","query":"반도체"}` | 201 `{"folder":{...}}` |
| PATCH `/api/favorites/folders/2` | `{"name":"반도체 모음"}` | 200 `{"folder":{...}}` |
| DELETE `/api/favorites/folders/2` | 없음 | 200 `{"ok":true,"moved_to_folder_id":1}` |

폴더 생성/변경 응답 예시:

```json
{"folder":{"id":2,"user_id":1,"name":"경제 뉴스","query":"반도체","is_default":false,"created_at":"2026-11-26T00:02:00Z"}}
```

목록은 ID 오름차순이며 각 폴더에 `last_saved_at`(현재 포함된 즐겨찾기의 최대 저장 시각, 비었으면 null)을 추가한다. 기본 폴더의 초기 이름은 `기본 폴더`, `query:null`, `is_default:true`다. PATCH에서 query 생략은 보존, `query:null`은 제거다.

기본 폴더는 이름 변경 가능하지만 삭제는 409. 일반 폴더 삭제는 기사를 기본 폴더로 옮기며 중복 기사는 기존 기본 폴더 행·검색어·시각을 유지한다. 이동된 행은 새 즐겨찾기 ID를 가질 수 있으므로 다시 조회한다. DB 직접 폴더 삭제는 CASCADE이므로 이동 규칙은 반드시 API를 통해 사용한다.

## 즐겨찾기

| 메서드/경로 | 요청 | 성공 |
|---|---|---|
| POST `/api/favorites` | `{"article_id":123,"folder_id":2,"saved_query":"반도체"}` | 201 `{"favorite":{...}}` |
| POST `/api/favorites/batch` | `{"article_ids":[123,456],"folder_id":2,"saved_query":"반도체"}` | 201 `{"favorites":[{...}]}` |
| GET `/api/favorites?folder_id=2&limit=100&offset=0` | 쿼리 | 200 아래 예시 |
| DELETE `/api/favorites?favorite_id=10` | 본문 없음 | 200 `{"ok":true}` |
| POST `/api/favorites/batch-delete` | `{"favorite_ids":[10,11]}` 또는 `{"article_ids":[123,456]}` | 200 `{"deleted":[{"id":10,"article_id":123}]}` |

단건 저장 응답 예시

```json
{"favorite":{"id":10,"user_id":1,"article_id":123,"folder_id":2,"saved_query":"반도체","created_at":"2026-11-26T00:03:00Z"}}
```

저장 요청에서 `folder_id` 생략/null은 기본 폴더를 선택한다. `saved_query`는 최대 500자 또는 null이다. ID는 1~9223372036854775807 범위의 JSON 정수다. 단건 저장은 같은 회원·기사·폴더의 즐겨찾기를 다시 저장하면 검색어만 갱신한다. 여러 폴더에 같은 기사를 저장할 수 있다.

배치 요청의 ID 배열은 1~500개이며, 중복 ID는 한 번만 처리한다. 저장은 현재 회원이 소유한 폴더에 한 트랜잭션으로 추가한다. 대상 기사나 폴더를 찾을 수 없으면 404를 반환하고 해당 요청의 추가를 모두 취소한다. 같은 폴더에 이미 저장된 행은 ID·검색어·시각을 유지하며 새 행과 함께 반환한다. 응답의 각 행과 정렬은 GET 목록과 같다.

배치 삭제는 `favorite_ids` 또는 `article_ids` 중 하나의 배열을 받는다. 즐겨찾기 ID를 지정하면 해당 행만 삭제한다. 기사 ID를 지정하면 현재 회원의 모든 폴더에서 일치하는 행을 삭제한다. 응답에는 실제 삭제된 행의 즐겨찾기 ID와 기사 ID가 들어간다. 일치하는 본인 행이 없으면 빈 배열을 반환한다. 두 배치 요청 모두 동일한 연결에서 회원 세션을 확인하고 작업을 수행하며 `X-DICE-CSRF: 1`을 요구한다.

GET은 folder_id 생략 시 모든 폴더, limit 1~500(기본 100), offset 0~2147483647(기본 0). 저장 시각 DESC, 즐겨찾기 ID DESC 순이다. total은 선택 범위의 전체 행 수이고 여러 폴더의 같은 기사도 별도 행이다.

```json
{"favorites":[{"id":10,"user_id":1,"article_id":123,"folder_id":2,"saved_query":"반도체","created_at":"2026-11-26T00:03:00Z","title":"예시 기사 제목","url":"https://example.test/news/123","published_date":"2026-11-25","publisher":"예시일보"}],"total":1,"limit":100,"offset":0}
```

기사 정보는 조회 시 조인한다. url/publisher는 null일 수 있다. 프론트는 링크의 HTTP/HTTPS 여부를 검사하고 제목은 텍스트로 렌더한다. 공용 기사 삭제 시 즐겨찾기도 삭제된다. 회원 ID는 세션에서만 결정한다. 요청에 제목·본문·URL·언론사·유사도·회원 ID가 있으면 422를 반환한다. 폴더 조회·수정·삭제와 즐겨찾기 단건 삭제에서 본인 소유의 대상을 찾을 수 없으면 404를 반환한다.

`프론트엔드/src/favorites.js` 연결 시 폴더 query/name, `lastSavedAt ← last_saved_at`, 기사 `id ← article_id`로 매핑한다. 즐겨찾기 행 ID는 삭제용으로 별도 보존한다. 검색어별 자동 폴더 선택은 프론트에서 query를 비교하고 없으면 생성한다. localStorage는 자동 업로드/삭제하지 않는다. 선택 기능 `/api/favorites/import`는 미구현이다. 필요하면 사용자 동의 후 실제 DB 기사 ID만 POST하고 전체 성공을 확인한 뒤 브라우저 데이터를 처리한다. ID 없는 과거 항목을 제목/URL로 임의 매칭하지 않는다.

## 오류

회원 API 오류는 `{"error":"설명"}`이며 비밀번호·입력 원문·SQL을 반사하지 않는다.

| 상태 | 의미/메시지 예시 |
|---|---|
| 401 | 세션 없음·만료 `로그인이 필요합니다.` / 로그인 실패 `아이디 또는 비밀번호가 올바르지 않습니다.` / 탈퇴 비밀번호 불일치 |
| 403 | CSRF 헤더 누락/불일치 |
| 404 | 없는 기사·폴더·즐겨찾기 또는 타인 데이터 |
| 409 | 중복 아이디 `이미 사용 중인 아이디입니다.` / 중복 폴더명 / 기본 폴더 삭제 |
| 422 | JSON·타입·길이·허용 문자 오류 또는 정의되지 않은 필드 |
| 429 | 전역 또는 인증 제한; Retry-After 참고 |
| 503 | DB 설정 없음·전용 역할 불일치·DB/스키마/권한/타임아웃 오류 |
| 500 | 그 밖의 내부 오류 (기존 미들웨어의 일반 메시지) |

## 검증 범위

백엔드에서 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider search`를 실행한다. 신규 테스트는 HTTP TestClient와 SQLite 메모리 어댑터를 사용하고 실제 PostgreSQL 연결을 차단한다. 마이그레이션의 유일성·FK·삭제 규칙을 메모리에서 검사하지만 PostgreSQL 실제 DDL·권한·행 잠금 경합 및 HTTPS 터널/브라우저 검증은 포함하지 않는다. 승인 후 Claude가 별도 환경에서 검증해야 한다.
