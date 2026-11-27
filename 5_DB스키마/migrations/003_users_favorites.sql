-- v0.2.163 / PostgreSQL 14+. 승인 후 관리자가 적용하며 앱은 자동 적용하지 않는다.
BEGIN;
SET LOCAL lock_timeout = '5s';

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
