-- DICE 뉴스 DB 스키마 (v0.2.163, PostgreSQL 14+ / pgvector)
-- 기사 제목은 articles에 한 번만 저장하고, article_categories에는 분류만 저장한다.
-- 기존 DB는 migrations/001_move_title_to_articles.sql로 이전한다.
-- 제목 임베딩: intfloat/multilingual-e5-base, query: 접두사, 정규화, VECTOR(768).
-- 한 트랜잭션으로 실행: 같은 이름의 테이블이 이미 있으면 오류로 전체가 취소되고 기존 데이터는 그대로 남는다.
BEGIN;

-- 사전 준비
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE articles (
    id BIGSERIAL NOT NULL,
    source_id TEXT NOT NULL,
    published_date DATE NOT NULL,
    title TEXT NOT NULL,
    loaded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    url TEXT,
    CONSTRAINT pk_articles PRIMARY KEY (id),
    CONSTRAINT uq_articles_source_id UNIQUE (source_id),
    CONSTRAINT uq_articles_url UNIQUE (url)
);

CREATE INDEX ix_articles_published_date ON articles (published_date);
CREATE INDEX ix_articles_title ON articles (title);
-- 제목 부분 일치 검색용 함수 기반 GIN 인덱스(search_title_normalize 등)는 별도 정의

CREATE TABLE article_keywords (
    article_id BIGINT NOT NULL,
    keyword TEXT NOT NULL,
    mention_count INTEGER NOT NULL,
    CONSTRAINT pk_article_keywords PRIMARY KEY (article_id, keyword),
    CONSTRAINT fk_article_keywords_article_id_articles
        FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT ck_article_keywords_mention_count_positive
        CHECK (mention_count >= 1)
);

-- 키워드 검색과 횟수 조건 검색(keyword = '검색어' AND mention_count >= 5)용
CREATE INDEX ix_article_keywords_keyword_count ON article_keywords (keyword, mention_count);

CREATE TABLE article_sources (
    article_id BIGINT NOT NULL,
    publisher TEXT NOT NULL,
    byline TEXT,
    CONSTRAINT pk_article_sources PRIMARY KEY (article_id),
    CONSTRAINT fk_article_sources_article_id_articles
        FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
);

CREATE INDEX ix_article_sources_publisher ON article_sources (publisher);

CREATE TABLE article_groups (
    article_id BIGINT NOT NULL,
    group_id BIGINT NOT NULL,
    is_representative BOOLEAN DEFAULT false NOT NULL,
    summary TEXT, -- 동아일보 요약 대상 행에만 저장. 원문 본문은 저장하지 않는다.
    CONSTRAINT pk_article_groups PRIMARY KEY (article_id),
    CONSTRAINT fk_article_groups_article_id_articles
        FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
);

CREATE INDEX ix_article_groups_group_id ON article_groups (group_id);
-- 묶음마다 대표 기사는 1건만 허용
CREATE UNIQUE INDEX uq_article_groups_representative
    ON article_groups (group_id) WHERE is_representative;

CREATE TABLE article_categories (
    article_id BIGINT NOT NULL,
    level INTEGER NOT NULL,
    major TEXT NOT NULL,
    minor TEXT,
    CONSTRAINT pk_article_categories PRIMARY KEY (article_id, level),
    CONSTRAINT ck_article_categories_level_range CHECK (level BETWEEN 1 AND 3),
    CONSTRAINT fk_article_categories_article_id_articles
        FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
);

CREATE TABLE article_embeddings (
    article_id BIGINT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding VECTOR(768) NOT NULL
);

-- 대량 적재 시 embed_articles.py --create-index로 적재 후 생성할 수 있다.
CREATE INDEX ix_article_embeddings_embedding_hnsw
    ON article_embeddings USING hnsw (embedding vector_cosine_ops);

-- 회원 및 기사 ID 참조 즐겨찾기 (003_users_favorites.sql과 동일)
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    login_id VARCHAR(20) NOT NULL UNIQUE CHECK (login_id ~ '^[a-z0-9_]{4,20}$'),
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE user_sessions (
    token_hash VARCHAR(64) PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX ix_user_sessions_expires_at ON user_sessions(expires_at);

CREATE TABLE favorite_folders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL CHECK (length(trim(name)) > 0),
    query VARCHAR(500),
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, name),
    UNIQUE(user_id, id)
);
CREATE UNIQUE INDEX uq_favorite_folders_default ON favorite_folders(user_id) WHERE is_default;

CREATE TABLE favorites (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    folder_id BIGINT NOT NULL,
    saved_query VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 타인의 폴더 참조는 DB에서도 차단. API는 폴더 삭제 전 기본 폴더로 이동한다.
    FOREIGN KEY (user_id, folder_id) REFERENCES favorite_folders(user_id, id) ON DELETE CASCADE,
    UNIQUE(user_id, article_id, folder_id)
);
CREATE INDEX ix_favorites_folder ON favorites(user_id, folder_id);
CREATE INDEX ix_favorites_article ON favorites(article_id);

COMMIT;
