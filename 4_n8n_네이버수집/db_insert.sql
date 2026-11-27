-- n8n "DB 저장" 노드(Postgres executeQuery). 쿼리 파라미터 1번 = 전처리 노드의 json.db(JSON 문자열).
-- 한 문장이라 전체가 한 트랜잭션. 이미 있는 기사는 기사·자식 모두 건너뛴다.
-- source_id는 'naver:' 접두사로 BigKinds와 구분한다. 본문·묶음은 넣지 않는다.
WITH d AS (SELECT $1::jsonb AS j),
a AS (
    SELECT * FROM jsonb_to_recordset((SELECT j->'articles' FROM d))
        AS x(id TEXT, source_id TEXT, published_date DATE, title TEXT, loaded_at TIMESTAMPTZ, url TEXT)
),
sources AS (
    SELECT * FROM jsonb_to_recordset((SELECT j->'article_sources' FROM d))
        AS x(article_id TEXT, publisher TEXT, byline TEXT)
),
ins AS (
    INSERT INTO articles (source_id, published_date, title, loaded_at, url)
    SELECT 'naver:' || a.source_id, a.published_date, a.title, a.loaded_at, NULLIF(a.url, '')
    FROM a JOIN sources src ON src.article_id=a.id
    WHERE NOT EXISTS (
        SELECT 1 FROM articles existing JOIN article_sources es ON es.article_id=existing.id
        WHERE existing.published_date=a.published_date AND es.publisher=src.publisher
          AND btrim(regexp_replace(normalize(existing.title), U&'[\0009-\000D\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000\FEFF]+', ' ', 'g'))
            = btrim(regexp_replace(normalize(a.title), U&'[\0009-\000D\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000\FEFF]+', ' ', 'g'))
    )
    ON CONFLICT DO NOTHING
    RETURNING id, source_id
),
m AS (SELECT a.id AS nid, ins.id FROM ins JOIN a ON 'naver:' || a.source_id = ins.source_id),
s AS (
    INSERT INTO article_sources (article_id, publisher, byline)
    SELECT m.id, x.publisher, NULLIF(x.byline, '')
    FROM sources x
    JOIN m ON m.nid = x.article_id
    ON CONFLICT DO NOTHING
    RETURNING 1
),
k AS (
    INSERT INTO article_keywords (article_id, keyword, mention_count)
    SELECT m.id, x.keyword, x.mention_count
    FROM jsonb_to_recordset((SELECT j->'article_keywords' FROM d)) AS x(article_id TEXT, keyword TEXT, mention_count INTEGER)
    JOIN m ON m.nid = x.article_id
    WHERE x.mention_count >= 1
    ON CONFLICT DO NOTHING
    RETURNING 1
),
c AS (
    INSERT INTO article_categories (article_id, level, major, minor)
    SELECT m.id, x.level, x.major, NULLIF(x.minor, '')
    FROM jsonb_to_recordset((SELECT j->'article_categories' FROM d)) AS x(article_id TEXT, level INTEGER, major TEXT, minor TEXT)
    JOIN m ON m.nid = x.article_id
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT (SELECT count(*) FROM a) AS input_articles,
       (SELECT count(*) FROM ins) AS new_articles,
       (SELECT count(*) FROM a) - (SELECT count(*) FROM ins) AS skipped,
       (SELECT count(*) FROM s) AS sources,
       (SELECT count(*) FROM k) AS keywords,
       (SELECT count(*) FROM c) AS categories;
