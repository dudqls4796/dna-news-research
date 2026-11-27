"""기사 제목을 로컬 CUDA로 임베딩하고 PostgreSQL에 COPY 적재한다.

기본 접속은 백엔드/.env이며 load_bigkinds.py의 접속 방식을 재사용한다.
BIGKINDS_DSN이 --env-file/BIGKINDS_ENV_FILE보다 우선한다.
모델만 공식 Hugging Face 저장소에서 받으며 기사 제목은 외부로 보내지 않는다.

--limit N은 ID순 앞 N개 기사(기존 임베딩/제목 누락 포함)로 범위를 고정한다.
같은 명령을 재실행하면 해당 범위의 미적재 기사만 처리한다.
기본 2,048개 후보마다 커밋하며 실패한 배치는 재실행으로 복구한다.
--create-index는 임베딩 없이 HNSW 인덱스만 생성하는 별도 단계이다.

예: python 백엔드/db/embed_articles.py --env-file 로컬설정경로 --limit 20000
    python 백엔드/db/embed_articles.py --env-file 로컬설정경로 --create-index
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import warnings
from pathlib import Path

import psycopg

from load_bigkinds import DEFAULT_ENV, ROOT, LoadError, connection_options, nonnegative


MODEL_NAME = "intfloat/multilingual-e5-base"
MODEL_REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"
DIMENSIONS = 768
DEFAULT_CACHE = ROOT / "백엔드/.venv/.cache/huggingface"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id BIGINT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding VECTOR(768) NOT NULL
)
"""
CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS ix_article_embeddings_embedding_hnsw
ON article_embeddings USING hnsw (embedding vector_cosine_ops)
"""
SELECT_PENDING = """
SELECT a.id, a.title
FROM articles a
WHERE a.id > %s AND a.id <= %s
  AND NOT EXISTS (
      SELECT 1 FROM article_embeddings e WHERE e.article_id = a.id
  )
ORDER BY a.id LIMIT %s
"""


@contextlib.contextmanager
def quiet_libraries():
    """라이브러리의 경고/원본 예외/진행 표시로 입력이 노출되지 않게 한다."""
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with open(os.devnull, "w") as sink, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    finally:
        logging.disable(previous)


def load_model(cache_dir: Path):
    # 제목을 읽기 전에 다운로드를 끝낸다. 모델 코드는 원격 실행하지 않는다.
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    with quiet_libraries():
        import torch
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError
        from sentence_transformers import SentenceTransformer

        if not torch.cuda.is_available():
            raise LoadError("CUDA를 사용할 수 없습니다.")
        options = dict(
            repo_id=MODEL_NAME, revision=MODEL_REVISION,
            cache_dir=str(cache_dir), endpoint="https://huggingface.co", token=False,
            allow_patterns=[
                "*.json", "*.safetensors", "sentencepiece.bpe.model", "tokenizer.model",
            ],
            ignore_patterns=["onnx/*", "openvino/*"],
        )
        try:
            path = snapshot_download(**options, local_files_only=True)
            # 중단된 다운로드의 불완전한 snapshot도 다시 받는다.
            if not (Path(path) / "model.safetensors").is_file():
                path = snapshot_download(**options)
        except LocalEntryNotFoundError:
            path = snapshot_download(**options)
        model = SentenceTransformer(
            path, device="cuda", local_files_only=True, trust_remote_code=False,
        )
        if model.get_sentence_embedding_dimension() != DIMENSIONS:
            raise LoadError("모델의 임베딩 차원이 일치하지 않습니다.")
    return model


def encode_titles(model, titles: list[str], batch_size: int):
    # 원격 호출 기능을 사용하지 않는 로컬 SentenceTransformer.encode이다.
    with quiet_libraries():
        import numpy as np

        vectors = model.encode(
            ["query: " + title for title in titles], prompt="",
            batch_size=batch_size, device="cuda", show_progress_bar=False,
            normalize_embeddings=True, convert_to_numpy=True, precision="float32",
        )
        if vectors.shape != (len(titles), DIMENSIONS):
            raise LoadError("임베딩 결과의 차원이 올바르지 않습니다.")
        if not np.isfinite(vectors).all() or not np.allclose(
            np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5, rtol=0,
        ):
            raise LoadError("임베딩 결과의 정규화가 올바르지 않습니다.")
    return vectors


def insert_vectors(conn: psycopg.Connection, ids: list[int], vectors) -> int:
    # COPY는 충돌 처리가 없으므로 임시 테이블에서 멱등 INSERT로 옮긴다.
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy(
            "COPY _embedding_batch (article_id, embedding) FROM STDIN"
        ) as copy:
            for article_id, vector in zip(ids, vectors, strict=True):
                value = "[" + ",".join(str(float(x)) for x in vector) + "]"
                copy.write_row((article_id, value))
        cur.execute("""
            INSERT INTO article_embeddings (article_id, embedding)
            SELECT article_id, embedding FROM _embedding_batch ORDER BY article_id
            ON CONFLICT (article_id) DO NOTHING
        """)
        return cur.rowcount


def embed_articles(
    conn: psycopg.Connection, limit: int | None, batch_size: int,
    commit_size: int, cache_dir: Path,
) -> dict[str, int]:
    # 상한 ID를 먼저 고정해 실행 중 추가되는 기사는 다음 실행에서 처리한다.
    upper_id = conn.execute("""
        SELECT max(id) FROM (SELECT id FROM articles ORDER BY id LIMIT %s) scope
    """, (limit,)).fetchone()[0]
    counts = dict(read=0, skipped=0, inserted=0)
    model = None
    last_id = -9223372036854775808
    conn.execute("""
        CREATE TEMP TABLE _embedding_batch (
            article_id BIGINT PRIMARY KEY, embedding VECTOR(768) NOT NULL
        ) ON COMMIT DELETE ROWS
    """)
    while upper_id is not None:
        # 제목을 메모리에 읽기 전에 모델 다운로드/로컬 로드를 끝낸다.
        pending = conn.execute("""
            SELECT EXISTS (
                SELECT 1 FROM articles a
                WHERE a.id > %s AND a.id <= %s AND NOT EXISTS (
                    SELECT 1 FROM article_embeddings e WHERE e.article_id = a.id
                )
            )
        """, (last_id, upper_id)).fetchone()[0]
        if not pending:
            break
        if model is None:
            model = load_model(cache_dir)
        rows = conn.execute(SELECT_PENDING, (last_id, upper_id, commit_size)).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        counts["read"] += len(rows)
        valid = [(article_id, title) for article_id, title in rows if title and title.strip()]
        counts["skipped"] += len(rows) - len(valid)
        if valid:
            ids, titles = zip(*valid)
            vectors = encode_titles(model, list(titles), batch_size)
            counts["inserted"] += insert_vectors(conn, list(ids), vectors)
        print_counts(counts)
    if not counts["read"]:
        print_counts(counts)
    return counts


def print_counts(counts: dict[str, int]) -> None:
    print(
        f"읽음={counts['read']} 제목없음={counts['skipped']} 신규임베딩={counts['inserted']}",
        flush=True,
    )


def positive(value: str) -> int:
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV))),
        help="접속 설정 파일 (BIGKINDS_DSN이 있으면 무시)",
    )
    parser.add_argument("--limit", type=nonnegative, help="ID순 앞 N개 기사로 처리 범위 고정")
    parser.add_argument("--batch-size", type=positive, default=64, help="GPU 인코딩 배치")
    parser.add_argument("--commit-size", type=positive, default=2048, help="COPY/커밋 단위")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="모델 캐시 경로")
    parser.add_argument("--create-index", action="store_true", help="HNSW 인덱스만 생성")
    args = parser.parse_args()
    try:
        with psycopg.connect(
            **connection_options(args.env_file), autocommit=True,
            connect_timeout=10, application_name="article_title_embedder",
        ) as conn:
            with conn.transaction():
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(CREATE_TABLE)
            if args.create_index:
                conn.execute(CREATE_INDEX)
                print("인덱스확인=1", flush=True)
            else:
                embed_articles(conn, args.limit, args.batch_size, args.commit_size, args.cache_dir)
    except LoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("임베딩 중단: 진행 중인 배치는 롤백됩니다.", file=sys.stderr)
        return 130
    except Exception:
        # DB/COPY/모델 예외에는 제목이나 접속값이 포함될 수 있다.
        print("임베딩 실패: 모델 캐시·CUDA·DB 연결·권한·스키마를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
