-- PostgreSQL. 백업 후 실행한다. 네이버 기사 분류만 바꾸며 제목이나 다른 기사 데이터는 읽거나 수정하지 않는다.
BEGIN;

-- 이전 집계와 행 재구성 사이에 분류를 쓰는 다른 트랜잭션을 대기시킨다. 조회는 가능하다.
LOCK TABLE article_categories IN SHARE ROW EXCLUSIVE MODE;

SELECT 'before' AS phase, c.level, c.major, c.minor, count(*) AS rows
FROM article_categories AS c
JOIN articles AS a ON a.id = c.article_id
WHERE a.source_id LIKE 'naver:%'
GROUP BY c.level, c.major, c.minor
ORDER BY c.level, c.major, c.minor;

-- v0.2.92까지의 분류 사전과 v0.2.94의 스포츠 추가분, 과거 제목 미일치 기본값을 포함한다.
-- 예전 한 분류가 여러 새 소분류를 포괄할 때는 아래 대표 분류로 이전한다.
CREATE TEMP TABLE naver_bigkinds_category_rows ON COMMIT DROP AS
WITH category_map (old_major, old_minor, new_major, new_minor) AS (
    VALUES
        ('정치', '대통령실', '정치', '청와대'),
        ('정치', '국회·정당', '정치', '국회_정당'),
        ('정치', '북한', '정치', '북한'),
        ('정치', '행정', '정치', '행정_자치'),
        ('정치', '국방·외교', '정치', '외교'),
        ('정치', '정치 일반', '정치', '정치일반'),
        ('경제', '금융', '경제', '금융_재테크'),
        ('경제', '증권', '경제', '증권_증시'),
        ('경제', '산업·재계', '경제', '산업_기업'),
        ('경제', '중기·벤처', '경제', '산업_기업'),
        ('경제', '부동산', '경제', '부동산'),
        ('경제', '글로벌 경제', '경제', '국제경제'),
        ('경제', '생활경제', '경제', '경제일반'),
        ('경제', '경제 일반', '경제', '경제일반'),
        ('사회', '사건사고', '사회', '사건_사고'),
        ('사회', '교육', '사회', '교육_시험'),
        ('사회', '노동', '사회', '노동_복지'),
        ('사회', '언론', '사회', '미디어'),
        ('사회', '환경', '사회', '환경'),
        ('사회', '인권·복지', '사회', '노동_복지'),
        ('사회', '식품·의료', '사회', '의료_건강'),
        ('사회', '지역', '사회', '사회일반'),
        ('사회', '사회 일반', '사회', '사회일반'),
        ('생활·문화', '건강정보', '사회', '의료_건강'),
        ('생활·문화', '자동차·시승기', '경제', '자동차'),
        ('생활·문화', '도로·교통', '사회', '사회일반'),
        ('생활·문화', '여행·레저', '문화', '요리_여행'),
        ('생활·문화', '음식·맛집', '문화', '요리_여행'),
        ('생활·문화', '패션·뷰티', '문화', '생활'),
        ('생활·문화', '공연·전시', '문화', '전시_공연'),
        ('생활·문화', '책', '문화', '출판'),
        ('생활·문화', '종교', '문화', '종교'),
        ('생활·문화', '날씨', '사회', '날씨'),
        ('생활·문화', '생활·문화 일반', '문화', '문화일반'),
        ('세계', '아시아·호주', '국제', '아시아'),
        ('세계', '미국·중남미', '국제', '미국_북미'),
        ('세계', '유럽', '국제', '유럽_EU'),
        ('세계', '중동·아프리카', '국제', '중동_아프리카'),
        ('세계', '세계 일반', '국제', '국제일반'),
        ('IT·과학', '모바일', 'IT_과학', '모바일'),
        ('IT·과학', '인터넷·SNS', 'IT_과학', '인터넷_SNS'),
        ('IT·과학', '통신·뉴미디어', 'IT_과학', '모바일'),
        ('IT·과학', 'IT 일반', 'IT_과학', 'IT_과학일반'),
        ('IT·과학', '보안·해킹', 'IT_과학', '보안'),
        ('IT·과학', '컴퓨터', 'IT_과학', 'IT_과학일반'),
        ('IT·과학', '게임·리뷰', 'IT_과학', '콘텐츠'),
        ('IT·과학', '과학 일반', 'IT_과학', '과학'),
        ('IT·과학', 'IT·과학 일반', 'IT_과학', 'IT_과학일반'),
        ('스포츠', '축구', '스포츠', '축구'),
        ('스포츠', '야구', '스포츠', '야구'),
        ('스포츠', '골프', '스포츠', '골프'),
        ('스포츠', '농구_배구', '스포츠', '농구_배구'),
        ('스포츠', '올림픽_아시안게임', '스포츠', '올림픽_아시안게임'),
        ('스포츠', '스포츠일반', '스포츠', '스포츠일반')
), affected AS (
    SELECT DISTINCT c.article_id
    FROM article_categories AS c
    JOIN category_map AS m ON (c.major, c.minor) = (m.old_major, m.old_minor)
    JOIN articles AS a ON a.id = c.article_id
    WHERE a.source_id LIKE 'naver:%'
      AND (c.major, c.minor) IS DISTINCT FROM (m.new_major, m.new_minor)
), mapped AS (
    SELECT c.article_id, c.level, COALESCE(m.new_major, c.major) AS major, COALESCE(m.new_minor, c.minor) AS minor
    FROM article_categories AS c
    JOIN affected AS a ON a.article_id = c.article_id
    LEFT JOIN category_map AS m ON (c.major, c.minor) = (m.old_major, m.old_minor)
), deduplicated AS (
    SELECT article_id, major, minor, MIN(level) AS first_level
    FROM mapped
    GROUP BY article_id, major, minor
)
SELECT article_id, (row_number() OVER (PARTITION BY article_id ORDER BY first_level))::integer AS level, major, minor
FROM deduplicated;

-- 같은 새 분류로 합쳐진 행은 가장 높은 순위를 유지한다. 기사별로 1부터 순위를 다시 매긴다.
-- 기존 키를 먼저 지우고 새 키를 넣으므로 (article_id, level) 충돌이 없다.
DELETE FROM article_categories AS c
USING (SELECT DISTINCT article_id FROM naver_bigkinds_category_rows) AS migrated
WHERE c.article_id = migrated.article_id;

INSERT INTO article_categories (article_id, level, major, minor)
SELECT article_id, level, major, minor FROM naver_bigkinds_category_rows;

SELECT 'after' AS phase, c.level, c.major, c.minor, count(*) AS rows
FROM article_categories AS c
JOIN articles AS a ON a.id = c.article_id
WHERE a.source_id LIKE 'naver:%'
GROUP BY c.level, c.major, c.minor
ORDER BY c.level, c.major, c.minor;

COMMIT;
