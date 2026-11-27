-- 기존 행/컬럼은 변경하지 않고 NULL 허용 컬럼만 추가한다.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE article_groups ADD COLUMN IF NOT EXISTS summary TEXT;
COMMIT;
