// 실행: node build_workflow.js → naver_news_csv_workflow.json (n8n "Import from File"용)
const fs = require('fs');
const code = f => fs.readFileSync(`${__dirname}/${f}`, 'utf8');
const withCommon = f => code('news_common.js') + '\n' + code(f);
const wf = {
  name: '네이버에서 뉴스수집',
  settings: { executionOrder: 'v1', timezone: 'Asia/Seoul', executionTimeout: 3300 },
  nodes: [
    { id: 'n1', name: '매시간 정각', type: 'n8n-nodes-base.scheduleTrigger', typeVersion: 1.2, position: [-660, 0],
      parameters: { rule: { interval: [{ field: 'cronExpression', expression: '0 * * * *' }] } } },
    { id: 'n2', name: '검색 요청 만들기', type: 'n8n-nodes-base.code', typeVersion: 2, position: [220, 0],
      parameters: { jsCode: withCommon('build_requests.js') } },
    { id: 'n3', name: '네이버 뉴스 검색', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: [440, 0],
      parameters: {
        url: 'https://naverapihub.apigw.ntruss.com/search/v1/news', // NAVER API HUB(2026 이관). 헤더: X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY,
        authentication: 'genericCredentialType', genericAuthType: 'httpCustomAuth',
        sendQuery: true,
        queryParameters: { parameters: [
          { name: 'query', value: '={{ $json.keyword }}' }, { name: 'display', value: '={{ $json.display }}' },
          { name: 'start', value: '={{ $json.start }}' }, { name: 'sort', value: 'date' } ] },
        options: { timeout: 10000, batching: { batch: { batchSize: 5, batchInterval: 1000 } },
          redirect: { redirect: { followRedirects: false } } },
      },
      credentials: { httpCustomAuth: { id: 'ptRshOfVJHFFbtFZ', name: '네이버 수집' } }, // 로컬 n8n의 Credential 참조(키 값 아님)
      retryOnFail: false, onError: 'continueRegularOutput',
    },
    { id: 'n13', name: '검색 결과 정리', type: 'n8n-nodes-base.code', typeVersion: 2, position: [660, 0],
      parameters: { jsCode: withCommon('search_results.js') } },
    { id: 'n14', name: '다음 검색 쪽 여부', type: 'n8n-nodes-base.if', typeVersion: 2.2, position: [880, 0],
      parameters: { conditions: { options: { version: 2 }, combinator: 'and', conditions: [
        { leftValue: '={{ $json.hasNext }}', operator: { type: 'boolean', operation: 'true', singleValue: true } },
      ] } } },
    { id: 'n6', name: '원문 대상 만들기', type: 'n8n-nodes-base.code', typeVersion: 2, position: [1100, 0],
      parameters: { jsCode: withCommon('prepare_articles.js') } },
    { id: 'n7', name: '기사별 순회', type: 'n8n-nodes-base.splitInBatches', typeVersion: 3, position: [1320, 0],
      parameters: { batchSize: 1, options: {} } },
    { id: 'n8', name: '원문 분석(본문 미저장)', type: 'n8n-nodes-base.code', typeVersion: 2, position: [1540, 360],
      notes: '원문 요청·디코딩·집계를 이 Code 노드 메모리에서만 수행. runner 내장 모듈 http,https,url 허용 필요. 원문 HTTP 노드로 분리 금지.',
      notesInFlow: true, parameters: { jsCode: withCommon('enrich_articles.js') } },
    { id: 'n4', name: '전처리·CSV 만들기', type: 'n8n-nodes-base.code', typeVersion: 2, position: [1540, 0],
      notes: 'v0.1.74 CSV: 제목은 articles.title에 저장하고 article_categories에는 분류만 저장한다. DB 이전 후 가져오기.',
      notesInFlow: true, parameters: { jsCode: withCommon('preprocess.js') } },
    { id: 'n5', name: 'CSV 저장', type: 'n8n-nodes-base.readWriteFile', typeVersion: 1, position: [1760, 0], alwaysOutputData: true,
      parameters: { operation: 'write', fileName: '=/home/node/naver_csv/{{ $binary.data.fileName }}', dataPropertyName: 'data', options: {} } },
    // Credential은 사용자가 n8n UI에서 직접 만들고 선택한다(비밀번호를 JSON에 넣지 않음).
    { id: 'n9', name: 'DB 저장', type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: [1760, 180], executeOnce: true,
      notes: 'db_insert.sql. $1 = 전처리 첫 항목의 json.db. 기사·출처·키워드·분류만 저장(source_id에 naver: 접두사).',
      notesInFlow: true,
      parameters: { operation: 'executeQuery', query: code('db_insert.sql'),
        options: { queryReplacement: '={{ JSON.stringify($json.db) }}' } } },
    // 이 PC의 백엔드/db/embed_server.py(127.0.0.1:8011)를 호출해 임베딩 없는 기사(새 네이버 기사 포함)를 임베딩한다.
    { id: 'n10', name: '임베딩 요청', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: [1980, 180], alwaysOutputData: true,
      notes: '서버가 꺼져 있어도 수집은 성공으로 끝나고, 다음 호출 때 밀린 기사까지 임베딩한다.', notesInFlow: true,
      parameters: { method: 'POST', url: 'http://host.docker.internal:8011/embed',
        sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Embed-Trigger', value: 'n8n' }] },
        options: { timeout: 3600000 } },
      onError: 'continueRegularOutput' },
    { id: 'n15', name: '저장 분기 완료', type: 'n8n-nodes-base.merge', typeVersion: 3, position: [2200, 0], parameters: { mode: 'append' } },
    { id: 'n16', name: '수집 요약', type: 'n8n-nodes-base.code', typeVersion: 2, position: [2420, 0],
      parameters: { jsCode: `const { window_id, stats } = $('원문 대상 만들기').first(0, 0).json;
return [{ json: { status: 'finished', window_id, stats } }];` } },
  ],
  connections: {
    '매시간 정각': { main: [[{ node: '검색 요청 만들기', type: 'main', index: 0 }]] },
    '검색 요청 만들기': { main: [[{ node: '네이버 뉴스 검색', type: 'main', index: 0 }]] },
    '네이버 뉴스 검색': { main: [[{ node: '검색 결과 정리', type: 'main', index: 0 }]] },
    '검색 결과 정리': { main: [[{ node: '다음 검색 쪽 여부', type: 'main', index: 0 }]] },
    '다음 검색 쪽 여부': { main: [
      [{ node: '검색 요청 만들기', type: 'main', index: 0 }],
      [{ node: '원문 대상 만들기', type: 'main', index: 0 }],
    ] },
    '원문 대상 만들기': { main: [[{ node: '기사별 순회', type: 'main', index: 0 }]] },
    '기사별 순회': { main: [
      [{ node: '전처리·CSV 만들기', type: 'main', index: 0 }],
      [{ node: '원문 분석(본문 미저장)', type: 'main', index: 0 }],
    ] },
    '원문 분석(본문 미저장)': { main: [[{ node: '기사별 순회', type: 'main', index: 0 }]] },
    '전처리·CSV 만들기': { main: [[{ node: 'CSV 저장', type: 'main', index: 0 }, { node: 'DB 저장', type: 'main', index: 0 }]] },
    'DB 저장': { main: [[{ node: '임베딩 요청', type: 'main', index: 0 }]] },
    'CSV 저장': { main: [[{ node: '저장 분기 완료', type: 'main', index: 0 }]] },
    '임베딩 요청': { main: [[{ node: '저장 분기 완료', type: 'main', index: 1 }]] },
    '저장 분기 완료': { main: [[{ node: '수집 요약', type: 'main', index: 0 }]] },
  },
};
fs.writeFileSync(`${__dirname}/naver_news_csv_workflow.json`, JSON.stringify(wf, null, 2));
console.log('written');
