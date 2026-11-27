-- v0.1.74: 기사별 최소 level의 제목을 articles로 옮긴다(PostgreSQL 14+).
-- 한 트랜잭션으로 실행하며, 제목 누락이나 의존성 오류 시 전체를 롤백한다.
-- 이미 이전된 DB는 NOTICE와 함께 건너뛴다. CASCADE는 사용하지 않는다.
BEGIN;

-- 이전 중 신규 기사/분류가 들어오거나 동시에 DDL이 실행되지 않게 한다.
LOCK TABLE articles, article_categories IN ACCESS EXCLUSIVE MODE;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'article_categories'::regclass
          AND attname = 'title' AND NOT attisdropped
    ) THEN
        IF EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = 'articles'::regclass
              AND attname = 'title' AND NOT attisdropped AND attnotnull
        ) THEN
            RAISE NOTICE '001_move_title_to_articles: 이미 적용되어 건너뜁니다.';
            RETURN;
        END IF;
        RAISE EXCEPTION '001_move_title_to_articles: 이전 전후 스키마가 아닙니다.';
    END IF;

    ALTER TABLE articles ADD COLUMN IF NOT EXISTS title TEXT;

    UPDATE articles AS a
    SET title = c.title
    FROM (
        SELECT DISTINCT ON (article_id) article_id, title
        FROM article_categories
        ORDER BY article_id, level
    ) AS c
    WHERE a.id = c.article_id;

    IF EXISTS (SELECT 1 FROM articles WHERE title IS NULL) THEN
        RAISE EXCEPTION '001_move_title_to_articles: 제목이 NULL인 기사가 있어 이전을 중단합니다.';
    END IF;

    ALTER TABLE articles ALTER COLUMN title SET NOT NULL;
    -- 기존 분류 제목 인덱스를 기사 제목 인덱스로 대체한다.
    CREATE INDEX IF NOT EXISTS ix_articles_title ON articles (title);
    ALTER TABLE article_categories DROP COLUMN title;
END
$migration$;

COMMIT;
