-- 삭제 대상과 연결 행 수를 먼저 표시한다. articles의 FK ON DELETE CASCADE로 연결 행도 삭제한다.
BEGIN ISOLATION LEVEL REPEATABLE READ;

CREATE TEMP TABLE cleanup_article_targets ON COMMIT DROP AS
WITH repeated_titles AS MATERIALIZED (
    SELECT title
    FROM articles
    GROUP BY title
    HAVING count(*) > 1
), ranked AS (
    SELECT a.id, row_number() OVER (PARTITION BY a.published_date, r.title, s.publisher ORDER BY a.id) AS position
    FROM repeated_titles r
    CROSS JOIN LATERAL (SELECT id, published_date FROM articles WHERE title=r.title OFFSET 0) a
    LEFT JOIN LATERAL (SELECT publisher FROM article_sources WHERE article_id=a.id OFFSET 0) s ON TRUE
)
SELECT id, position > 1 AS duplicate_article, id IN (690932, 537649, 45527, 73798) AS test_article
FROM ranked
WHERE (position > 1 OR id IN (690932, 537649, 45527, 73798)) AND id <> 156682
UNION ALL
SELECT a.id, false, true
FROM articles a
WHERE a.id IN (690932, 537649, 45527, 73798) AND NOT EXISTS (SELECT 1 FROM ranked r WHERE r.id=a.id);

SELECT count(*) FILTER (WHERE t.duplicate_article) AS duplicate_articles,
       count(*) FILTER (WHERE t.test_article) AS test_articles,
       count(*) AS articles,
       COALESCE(sum((SELECT count(*) FROM article_keywords WHERE article_id=t.id)), 0) AS article_keywords,
       COALESCE(sum((SELECT count(*) FROM article_sources WHERE article_id=t.id)), 0) AS article_sources,
       COALESCE(sum((SELECT count(*) FROM article_categories WHERE article_id=t.id)), 0) AS article_categories,
       COALESCE(sum((SELECT count(*) FROM article_embeddings WHERE article_id=t.id)), 0) AS article_embeddings,
       COALESCE(sum((SELECT count(*) FROM article_groups WHERE article_id=t.id)), 0) AS article_groups
FROM cleanup_article_targets t;

DELETE FROM articles a USING cleanup_article_targets t WHERE a.id=t.id;

COMMIT;
