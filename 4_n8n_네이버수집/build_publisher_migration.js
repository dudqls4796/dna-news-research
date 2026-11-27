// news_common.js의 사전을 SQL 이관 파일로 생성한다. DB에는 접속하지 않는다.
const fs = require('fs');
const path = require('path');
const common = fs.readFileSync(path.join(__dirname, 'news_common.js'), 'utf8');
const match = common.match(/const PUBLISHER_DATA = (\{[\s\S]*?\});\r?\nconst PUBLISHERS/);
if (!match) throw new Error('언론사 사전을 찾을 수 없습니다');
const domains = JSON.parse(match[1]).domains;
const quote = s => "'" + s.replace(/'/g, "''") + "'";
const values = Object.entries(domains).sort(([a], [b]) => a.localeCompare(b, 'en'))
  .map(([domain, name]) => `    (${quote(domain)}, ${quote(name)})`).join(',\n');
const cte = `WITH publisher_map(domain, name) AS (\n  VALUES\n${values}\n)\n`;
const sql = `-- news_common.js를 기준으로 build_publisher_migration.js가 생성. 실행 전 검토.\nBEGIN;\n\n${cte}SELECT count(*) AS affected_articles, count(DISTINCT s.publisher) AS affected_domains\nFROM article_sources s JOIN articles a ON a.id = s.article_id\nJOIN publisher_map m ON m.domain = s.publisher\nWHERE a.source_id LIKE 'naver:%' AND s.publisher IS DISTINCT FROM m.name;\n\n${cte}UPDATE article_sources s SET publisher = m.name\nFROM articles a, publisher_map m\nWHERE a.id = s.article_id AND a.source_id LIKE 'naver:%'\n  AND s.publisher = m.domain AND s.publisher IS DISTINCT FROM m.name;\n\nCOMMIT;\n`;
fs.writeFileSync(path.join(__dirname, '../백엔드/db/migrate_publisher_names.sql'), sql);
