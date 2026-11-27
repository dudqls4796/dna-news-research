"""BigKinds CSV의 허용 필드만 기존 PostgreSQL 스키마에 적재한다.

기본 입력은 저장소의 bigkinds_merged.csv, 접속 설정은 백엔드/.env이다.
--env-file 또는 BIGKINDS_ENV_FILE로 설정 파일을 바꿀 수 있다.
BIGKINDS_DSN을 설정하면 .env를 읽지 않고 해당 DSN만 사용한다.
비밀번호가 명령 이력에 남지 않도록 DSN 명령행 인자는 제공하지 않는다.

사용 예: python 백엔드/db/load_bigkinds.py --env-file 로컬설정경로 --limit 50000
자체 점검: python 백엔드/db/load_bigkinds.py --self-test (DB 접속 없음)

입력 50,000행마다 COPY 후 기사와 자식 데이터를 하나의 트랜잭션으로
커밋한다. 실패한 배치는 롤백되며 같은 CSV를 재실행하면 이어서 적재된다.
기존 기사와 충돌하면 관련 자식도 건너뛴다. 기존 데이터를 갱신하지 않는다.
본문·인물·위치·기관·특성추출은 CSV 파싱 외에 사용하거나 출력하지 않는다.
적재 후 신규기사와 키워드기사 건수를 비교한다. 키워드가 비어 있는 신규 기사가
있으면 원본의 키워드 열을 확인한다. 월별 주요 키워드는 검색 시 비중 상승을 계산하므로
적재 확인에는 사용하지 않는다. 별도 키워드 추출·집계 실행은 필요 없다.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from datetime import datetime
from itertools import islice
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "프로토타입/news_nlp/data/bigkinds/bigkinds_merged.csv"
DEFAULT_ENV = ROOT / "백엔드/.env"
BATCH_SIZE = 50_000
FIELDS = (
    "뉴스 식별자", "일자", "언론사", "기고자", "제목", "URL", "키워드",
    "통합 분류1", "통합 분류2", "통합 분류3", "분석제외 여부",
)
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+[ \t]*@[ \t]*[\w.-]+", re.UNICODE)
ROLES = re.compile(r"(?:수석|선임|수습|인턴|객원|명예|사진|취재|디지털|논설)?기자|특파원|통신원")


class LoadError(Exception):
    """원본 데이터나 접속 정보를 포함하지 않는 오류만 담는다."""


def clean_byline(value: str) -> str | None:
    """이메일, 기자 직함, 지역 서명을 제거하고 복수 기고자를 보존한다."""
    value = EMAIL.sub("", value)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\[[^\]]*\]", "", value)
    value = ROLES.sub("", value)
    value = re.sub(r"[()<>]", " ", value)
    names = []
    for part in re.split(r"[,;/·&\n\r]+", value):
        part = part.rsplit("=", 1)[-1]
        name = re.sub(r"\s+", " ", part).strip(" \t:-|\"'")
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or None


def top_keywords(value: str) -> list[dict[str, str | int]]:
    """횟수 내림차순, 동률은 CSV 첫 출현 순으로 최대 30개를 반환한다."""
    counts = Counter(word.strip() for word in value.split(",") if word.strip())
    return [
        {"keyword": word, "mention_count": count}
        for word, count in counts.most_common(30)
    ]


def category_values(row: dict[str, str]) -> list[dict[str, str | int | None]]:
    categories = []
    for level in range(1, 4):
        value = row[f"통합 분류{level}"]
        if value.strip():
            major, separator, minor = value.partition(">")
            categories.append({
                "level": level, "major": major,
                "minor": minor if separator else None,
            })
    return categories


def prepare_row(number: int, row: dict[str, str]) -> tuple | None:
    if row["분석제외 여부"].strip():
        return None
    if not all(row[key].strip() for key in ("뉴스 식별자", "일자", "언론사", "제목")):
        raise LoadError(f"필수값 누락: 입력 {number}건째")
    try:
        date_text = row["일자"].strip()
        if not re.fullmatch(r"\d{8}", date_text):
            raise ValueError
        published_date = datetime.strptime(date_text, "%Y%m%d").date()
    except ValueError:
        raise LoadError(f"일자 형식 오류: 입력 {number}건째") from None
    return (
        number, row["뉴스 식별자"].strip(), published_date,
        row["URL"].strip() or None, row["언론사"].strip(),
        clean_byline(row["기고자"]), row["제목"],
        Jsonb(top_keywords(row["키워드"])), Jsonb(category_values(row)),
    )


def connection_options(env_file: Path) -> dict:
    dsn = os.environ.get("BIGKINDS_DSN")
    if dsn:
        return {"conninfo": dsn}
    if not env_file.is_file():
        raise LoadError("접속 설정 파일이 없습니다.")
    values = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    mapping = {
        "POSTGRES_DB": "dbname", "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": "password", "DB_HOST": "host", "DB_PORT": "port",
    }
    if any(values.get(key) is None for key in mapping):
        raise LoadError("접속 설정에 필수 항목이 없습니다.")
    if any(not values[key] for key in mapping if key != "POSTGRES_PASSWORD"):
        raise LoadError("접속 설정의 필수 항목이 비어 있습니다.")
    return {target: values[key] for key, target in mapping.items()}


CREATE_STAGING = """
CREATE TEMP TABLE _bigkinds_batch (
    row_no BIGINT PRIMARY KEY,
    source_id TEXT NOT NULL,
    published_date DATE NOT NULL,
    url TEXT,
    publisher TEXT NOT NULL,
    byline TEXT,
    title TEXT NOT NULL,
    keywords JSONB NOT NULL,
    categories JSONB NOT NULL
) ON COMMIT DELETE ROWS;
CREATE TEMP TABLE _bigkinds_inserted (
    source_id TEXT PRIMARY KEY
) ON COMMIT DELETE ROWS;
CREATE TEMP TABLE _bigkinds_selected (
    row_no BIGINT PRIMARY KEY,
    article_id BIGINT UNIQUE NOT NULL
) ON COMMIT DELETE ROWS;
"""

INSERT_ARTICLES = """
WITH inserted AS (
    INSERT INTO articles (source_id, published_date, title, loaded_at, url)
    SELECT source_id, published_date, title, statement_timestamp(), url
    FROM _bigkinds_batch
    ORDER BY row_no
    ON CONFLICT DO NOTHING
    RETURNING source_id
)
INSERT INTO _bigkinds_inserted SELECT source_id FROM inserted;
"""

# CSV 순서로 INSERT하므로 같은 URL/식별자는 처음 삽입된 행을 채택한다.
# source_id로 BIGSERIAL의 실제 ID를 얻고, URL 충돌로 빠진 기사의 자식은 만들지 않는다.
SELECT_INSERTED = """
INSERT INTO _bigkinds_selected (row_no, article_id)
SELECT DISTINCT ON (b.source_id) b.row_no, a.id
FROM _bigkinds_batch b
JOIN _bigkinds_inserted i ON i.source_id = b.source_id
JOIN articles a ON a.source_id = b.source_id
WHERE a.url IS NOT DISTINCT FROM b.url
ORDER BY b.source_id, b.row_no;
"""

INSERT_KEYWORDS = """
    INSERT INTO article_keywords (article_id, keyword, mention_count)
    SELECT s.article_id, k.keyword, k.mention_count
    FROM _bigkinds_selected s JOIN _bigkinds_batch b USING (row_no)
    CROSS JOIN LATERAL jsonb_to_recordset(b.keywords)
        AS k(keyword TEXT, mention_count INTEGER)
    ON CONFLICT DO NOTHING
"""

INSERT_CHILDREN = (
    """
    INSERT INTO article_sources (article_id, publisher, byline)
    SELECT s.article_id, b.publisher, b.byline
    FROM _bigkinds_selected s JOIN _bigkinds_batch b USING (row_no)
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO article_categories (article_id, level, major, minor)
    SELECT s.article_id, c.level, c.major, c.minor
    FROM _bigkinds_selected s JOIN _bigkinds_batch b USING (row_no)
    CROSS JOIN LATERAL jsonb_to_recordset(b.categories)
        AS c(level INTEGER, major TEXT, minor TEXT)
    ON CONFLICT DO NOTHING
    """,
)


def insert_batch(conn: psycopg.Connection, batch: list[tuple]) -> tuple[int, int, int]:
    if not batch:
        return 0, 0, 0
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy("""
            COPY _bigkinds_batch
                (row_no, source_id, published_date, url, publisher, byline,
                 title, keywords, categories)
            FROM STDIN
        """) as copy:
            for row in batch:
                copy.write_row(row)
        # 임시 테이블에는 자동 통계 수집이 없으므로 배치마다 통계를 갱신한다.
        cur.execute("ANALYZE _bigkinds_batch")
        cur.execute(INSERT_ARTICLES)
        inserted = cur.rowcount
        if not inserted:
            return 0, 0, 0
        cur.execute("ANALYZE _bigkinds_inserted")
        cur.execute(SELECT_INSERTED)
        cur.execute("ANALYZE _bigkinds_selected")
        for statement in INSERT_CHILDREN:
            cur.execute(statement)
        cur.execute("""
            SELECT count(*), count(*) FILTER (WHERE jsonb_array_length(b.keywords) > 0),
                   COALESCE(sum(jsonb_array_length(b.keywords)), 0)
            FROM _bigkinds_selected s JOIN _bigkinds_batch b USING (row_no)
        """)
        selected, keyword_articles, expected_keywords = cur.fetchone()
        cur.execute(INSERT_KEYWORDS)
        keyword_rows = cur.rowcount
        if selected != inserted or keyword_rows != expected_keywords:
            raise LoadError("신규 기사 키워드 적재 건수가 원본과 일치하지 않아 배치를 롤백합니다.")
    return inserted, keyword_articles, keyword_rows


def load_csv(conn: psycopg.Connection, csv_path: Path, limit: int | None) -> None:
    # CSV는 한 행씩 읽으며 허용한 필드만 메모리 배치와 COPY로 전달한다.
    csv.field_size_limit(sys.maxsize)
    read = excluded = inserted = keyword_articles = keyword_rows = 0
    batch = []

    def flush_batch() -> None:
        nonlocal inserted, keyword_articles, keyword_rows
        new_articles, new_keyword_articles, new_keywords = insert_batch(conn, batch)
        inserted += new_articles
        keyword_articles += new_keyword_articles
        keyword_rows += new_keywords
        batch.clear()
        print(f"읽음={read} 제외={excluded} 신규기사={inserted} 키워드기사={keyword_articles} "
              f"키워드없음={inserted - keyword_articles} 키워드행={keyword_rows}", flush=True)

    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        if len(header) != len(set(header)) or not set(FIELDS).issubset(header):
            raise LoadError("CSV 헤더에 필수 컬럼이 없거나 중복 컬럼이 있습니다.")
        columns = [(name, header.index(name)) for name in FIELDS]
        with conn.transaction():
            conn.execute(CREATE_STAGING)
        for read, raw in enumerate(islice(reader, limit), 1):
            if len(raw) != len(header):
                raise LoadError(f"컬럼 수 오류: 입력 {read}건째")
            row = {name: raw[index] for name, index in columns}
            del raw
            prepared = prepare_row(read, row)
            if prepared is None:
                excluded += 1
            else:
                batch.append(prepared)
            if read % BATCH_SIZE == 0:
                flush_batch()
        if read % BATCH_SIZE or read == 0:
            flush_batch()


def self_test() -> None:
    assert top_keywords(" 가,나,가, ,다,나,가 ") == [
        {"keyword": "가", "mention_count": 3},
        {"keyword": "나", "mention_count": 2},
        {"keyword": "다", "mention_count": 1},
    ]
    assert len(top_keywords(",".join(f"단어{i}" for i in range(40)))) == 30
    assert top_keywords("") == []
    assert clean_byline("홍길동 기자 <hong@example.com>") == "홍길동"
    assert clean_byline("홍길동 기자 hong@ example.com") == "홍길동"
    assert clean_byline("[서울] 김민수 선임기자, 이영희 특파원") == "김민수, 이영희"
    assert clean_byline("서울=홍길동 기자 / 홍길동 기자") == "홍길동"
    assert clean_byline("기자 <only@example.com>") is None
    assert clean_byline("") is None
    row = dict.fromkeys(FIELDS, "")
    row.update({"뉴스 식별자": "test", "일자": "20261001", "언론사": "동아일보"})
    for title in ("1∼9월 수출", "10월 1~10일 수출", "정치 적대감", "1 9월 수출"):
        row["제목"] = title
        assert prepare_row(1, row)[6] == title  # 실제 CSV 제목은 임의로 보정하지 않는다.
    print("자체점검=13", flush=True)


def nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("0 이상의 정수가 필요합니다.") from None
    if number < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수가 필요합니다.")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="입력 CSV 경로")
    parser.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV))),
        help="접속 설정 파일 (BIGKINDS_DSN이 있으면 무시)",
    )
    parser.add_argument("--limit", type=nonnegative, help="제외 행을 포함한 앞 N행만 읽음")
    parser.add_argument("--self-test", action="store_true", help="DB 없이 assert 자체 점검")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            with psycopg.connect(
                **connection_options(args.env_file), autocommit=True,
                connect_timeout=10, application_name="bigkinds_loader",
            ) as conn:
                load_csv(conn, args.csv, args.limit)
    except LoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("적재 중단: 진행 중인 배치는 롤백됩니다.", file=sys.stderr)
        return 130
    except Exception:
        # psycopg 오류에는 SQL/COPY 값이나 접속 정보가 포함될 수 있다.
        # traceback과 원본 예외를 출력하지 않는다.
        print("적재 실패: 입력 형식과 DB 연결·권한·스키마를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
