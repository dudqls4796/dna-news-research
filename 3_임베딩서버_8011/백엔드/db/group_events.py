"""정규화된 제목 임베딩으로 1층 사건 묶음을 전체 재계산한다.

코사인 >= 0.92, 묶음 전체 published_date 폭 <= 3일인 대표 중심 묶음이다.
이웃 수 내림차순 / ID 오름차순으로 대표를 고르고, 대표를 포함하는
3일 창 중 미배정 이웃이 가장 많은 창(동률은 가장 이른 창)을 채택한다.
묶음 구성 후 동아일보 기사가 있으면 리더와 가장 유사한 기사를 대표로 한다.
혼자인 기사는 저장하지 않는다. group_id는 최종 대표 기사 ID이다.

기본 접속은 백엔드/.env. --env-file/BIGKINDS_ENV_FILE 및 최우선
BIGKINDS_DSN은 load_bigkinds.py와 같다. 접속값은 출력하지 않는다.
--start/--end는 양끝 날짜를 포함한다. 범위 실행도 기존 article_groups
전체를 교체하므로 범위 비교에는 --dry-run을 사용한다.
읽기는 일관된 스냅샷, 교체는 별도 단일 DELETE + COPY 트랜잭션이다.
GPU에는 날짜가 가까운 작은 블록만 올리고, 원본 float32 벡터는 임시
메모리 매핑 파일에 둔다. 임시 파일은 종료 시 삭제된다(약 72만 건 2.1 GiB).

예: python 백엔드/db/group_events.py --env-file 로컬설정경로 --dry-run
    python 백엔드/db/group_events.py --self-test  (DB/CUDA 접속 없음)
"""

from __future__ import annotations

import argparse
from array import array
from datetime import date
import os
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from load_bigkinds import DEFAULT_ENV, LoadError, connection_options


PREFERRED_PUBLISHER = "동아일보"
THRESHOLD = 0.92
WINDOW_DAYS = 3
DIMENSIONS = 768
READ_SIZE = 4096
DATE_FILTER = "(%s::date IS NULL OR a.published_date >= %s::date) AND " \
              "(%s::date IS NULL OR a.published_date <= %s::date)"


def read_articles(conn, start, end, backing_file):
    """제목 없이 ID/일자/언론사 여부/벡터를 같은 스냅샷에서 읽는다."""
    params = (start, start, end, end)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        count, missing = conn.execute(f"""
            SELECT count(e.article_id), count(*) - count(e.article_id)
            FROM articles a LEFT JOIN article_embeddings e ON e.article_id = a.id
            WHERE {DATE_FILTER}
        """, params).fetchone()
        print(f"대상임베딩={count} 임베딩없음={missing}", flush=True)
        ids = np.empty(count, dtype=np.int64)
        days = np.empty(count, dtype=np.int32)
        preferred = np.empty(count, dtype=np.bool_)
        vectors = (np.memmap(backing_file, dtype=np.float32, mode="w+",
                             shape=(count, DIMENSIONS)) if count else None)
        try:
            with conn.cursor(name="event_articles", binary=True) as cur:
                cur.itersize = READ_SIZE
                cur.execute(f"""
                    SELECT a.id, a.published_date, e.embedding, COALESCE(s.publisher = %s, false)
                    FROM articles a JOIN article_embeddings e ON e.article_id = a.id
                    LEFT JOIN article_sources s ON s.article_id = a.id
                    WHERE {DATE_FILTER} ORDER BY a.published_date, a.id
                """, (PREFERRED_PUBLISHER, *params))
                offset = 0
                while rows := cur.fetchmany(READ_SIZE):
                    stop = offset + len(rows)
                    ids[offset:stop] = [row[0] for row in rows]
                    days[offset:stop] = [row[1].toordinal() for row in rows]
                    preferred[offset:stop] = [row[3] for row in rows]
                    batch = np.asarray([row[2].to_numpy() for row in rows], dtype=np.float32)
                    if batch.shape != (len(rows), DIMENSIONS) or not np.isfinite(batch).all():
                        raise LoadError("임베딩 차원 또는 유한값 검사가 실패했습니다.")
                    if not np.allclose(np.linalg.norm(batch, axis=1), 1, atol=1e-5, rtol=0):
                        raise LoadError("정규화되지 않은 임베딩이 있습니다.")
                    vectors[offset:stop] = batch
                    offset = stop
                    print(f"읽음={offset}", flush=True)
                if offset != count:
                    raise LoadError("스냅샷 기사 건수가 일치하지 않습니다.")
        except BaseException:
            if vectors is not None:
                vectors._mmap.close()
            raise
    return ids, days, vectors, preferred, missing


def build_neighbors(vectors, days: np.ndarray, block_size: int):
    """프로토타입의 날짜별 GPU 행렬곱을 고정 크기 타일로 나눈다."""
    import torch

    if not torch.cuda.is_available():
        raise LoadError("CUDA를 사용할 수 없습니다.")
    # float16 경계 오차를 피하고 프로토타입과 같은 float32로 비교한다.
    previous_tf32 = torch.backends.cuda.matmul.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = False
    neighbors = [array("I") for _ in days]
    compared = edges = 0
    try:
        with torch.inference_mode():
            for day in np.unique(days):
                q0, q1 = np.searchsorted(days, [day, day + 1])
                k0, k1 = np.searchsorted(days, [day - WINDOW_DAYS, day + WINDOW_DAYS + 1])
                for qa in range(int(q0), int(q1), block_size):
                    qb = min(qa + block_size, int(q1))
                    query = torch.from_numpy(vectors[qa:qb]).to("cuda")
                    for ka in range(int(k0), int(k1), block_size):
                        kb = min(ka + block_size, int(k1))
                        keys = torch.from_numpy(vectors[ka:kb]).to("cuda")
                        scores = query @ keys.T
                        qi, ki = torch.nonzero(scores >= THRESHOLD, as_tuple=True)
                        left = qi.cpu().numpy() + qa
                        right = ki.cpu().numpy() + ka
                        valid = left != right
                        left, right = left[valid], right[valid]
                        # nonzero는 행 순서이다. 행별 compact uint32 배열로 보존한다.
                        boundaries = np.r_[0, np.flatnonzero(np.diff(left)) + 1, len(left)]
                        for lo, hi in zip(boundaries[:-1], boundaries[1:]):
                            if hi > lo:
                                neighbors[int(left[lo])].extend(right[lo:hi].tolist())
                        compared += (qb - qa) * (kb - ka)
                        edges += len(left)
                        del keys, scores, qi, ki
                    del query
                print(f"비교기사={int(q1)} 비교쌍={compared} 방향이웃={edges}", flush=True)
    finally:
        torch.backends.cuda.matmul.allow_tf32 = previous_tf32
    return neighbors, compared


def select_window(representative_day: int, candidate_days: np.ndarray) -> int:
    """대표를 포함하는 닫힌 3일 창. max는 첫 최댓값을 택해 이른 창 우선."""
    return max(
        range(int(representative_day) - WINDOW_DAYS, int(representative_day) + 1),
        key=lambda s: int(np.count_nonzero((candidate_days >= s) & (candidate_days <= s + WINDOW_DAYS))),
    )


def assign_groups(ids: np.ndarray, days: np.ndarray, neighbors):
    # -1은 미배정/단독. 이웃 수는 최초 그래프의 수로 고정한다.
    representatives = np.full(len(ids), -1, dtype=np.int64)
    order = sorted(range(len(ids)), key=lambda i: (-len(neighbors[i]), int(ids[i])))
    groups = 0
    for a in order:
        if representatives[a] >= 0 or not neighbors[a]:
            continue
        candidates = np.frombuffer(neighbors[a], dtype=np.uint32)
        candidates = candidates[representatives[candidates] < 0]
        if not len(candidates):
            continue
        candidate_days = days[candidates]
        start = select_window(int(days[a]), candidate_days)
        members = candidates[(candidate_days >= start) & (candidate_days <= start + WINDOW_DAYS)]
        if not len(members):
            continue
        representatives[a] = ids[a]
        representatives[members] = ids[a]
        groups += 1
    return representatives, groups


def choose_representatives(ids, days, vectors, preferred, leaders):
    """완성된 묶음의 구성원을 유지하며 최종 대표만 바꾼다."""
    result = leaders.copy()
    selected = np.flatnonzero(leaders >= 0)
    if not len(selected):
        return result, 0
    selected = selected[np.argsort(leaders[selected], kind="stable")]
    boundaries = np.r_[0, np.flatnonzero(np.diff(leaders[selected])) + 1, len(selected)]
    id_to_index = {int(article_id): i for i, article_id in enumerate(ids)}
    preferred_groups = 0
    for lo, hi in zip(boundaries[:-1], boundaries[1:]):
        members = selected[lo:hi]
        candidates = members[preferred[members]]
        if not len(candidates):
            continue
        leader_index = id_to_index[int(leaders[members[0]])]
        candidate_vectors = np.asarray(vectors[candidates], dtype=np.float64)
        leader_vector = np.asarray(vectors[leader_index], dtype=np.float64)
        scores = (candidate_vectors @ leader_vector /
                  (np.linalg.norm(candidate_vectors, axis=1) * np.linalg.norm(leader_vector)))
        best = min(range(len(candidates)),
                   key=lambda i: (-float(scores[i]), int(days[candidates[i]]), int(ids[candidates[i]])))
        result[members] = ids[candidates[best]]
        preferred_groups += 1
    return result, preferred_groups


def replace_groups(conn, ids: np.ndarray, representatives: np.ndarray) -> int:
    """실패/중단 시 DELETE까지 롤백한다. 독자는 교체 전/후 완성본만 본다."""
    selected = np.flatnonzero(representatives >= 0)
    selected = selected[np.argsort(ids[selected])]
    with conn.transaction(), conn.cursor() as cur:
        # 동시 교체/INSERT가 DELETE 스냅샷과 엇갈리지 않게 직렬화한다.
        cur.execute("LOCK TABLE article_groups IN EXCLUSIVE MODE")
        cur.execute("SELECT article_id, summary FROM article_groups WHERE summary IS NOT NULL")
        summaries = dict(cur.fetchall())
        cur.execute("DELETE FROM article_groups")
        with cur.copy("COPY article_groups (article_id, group_id, is_representative, summary) FROM STDIN") as copy:
            for index in selected:
                article_id, group_id = int(ids[index]), int(representatives[index])
                copy.write_row((article_id, group_id, article_id == group_id, summaries.get(article_id)))
    return len(selected)


def group_events(conn, start=None, end=None, dry_run=False, block_size=2048):
    register_vector(conn)
    began = time.perf_counter()
    with tempfile.TemporaryFile() as backing_file:
        ids, days, vectors, preferred, missing = read_articles(conn, start, end, backing_file)
        loaded = time.perf_counter()
        try:
            if len(ids):
                neighbors, compared = build_neighbors(vectors, days, block_size)
            else:
                neighbors, compared = [], 0
            matched = time.perf_counter()
            representatives, groups = assign_groups(ids, days, neighbors)
            del neighbors
            representatives, preferred_groups = choose_representatives(
                ids, days, vectors, preferred, representatives)
            assigned = time.perf_counter()
            grouped = int(np.count_nonzero(representatives >= 0))
            written = 0 if dry_run else replace_groups(conn, ids, representatives)
            finished = time.perf_counter()
            print(f"묶음={groups} 묶인기사={grouped} 단독기사={len(ids) - grouped} 저장행={written} 동아일보 대표 묶음 수={preferred_groups}", flush=True)
            # 측정값은 호출자용이며 CLI 출력은 건수로만 제한한다.
            return dict(embedded=len(ids), missing=missing, groups=groups, grouped=grouped,
                        single=len(ids) - grouped, written=written, compared=compared,
                        preferred_groups=preferred_groups,
                        read_seconds=loaded - began, compare_seconds=matched - loaded,
                        assign_seconds=assigned - matched, write_seconds=finished - assigned,
                        seconds=finished - began)
        finally:
            if vectors is not None:
                vectors._mmap.close()


def self_test() -> None:
    from unittest.mock import MagicMock, call

    # 양쪽으로 연결되어도 전체 폭은 3일. 같은 수면 이른 창, 대표 포함.
    assert select_window(10, np.array([7, 8, 12, 13])) == 7
    assert select_window(10, np.array([7, 11, 12, 13])) == 10
    assert select_window(10, np.array([], dtype=np.int32)) == 7
    assert select_window(10, np.array([7, 10, 13])) == 7
    ids = np.array([30, 20, 10, 40, 50, 60, 70])
    days = np.array([2, 1, 1, 2, 3, 4, 5])
    vectors = np.array([[1, 0], [0.8, 0.6], [0.8, 0.6], [0.9, 0.4358899],
                        [1, 0], [0, 1], [1, 0]], dtype=np.float32)
    leaders = np.array([30, 30, 30, 30, 50, 50, -1])
    preferred = np.array([False, True, True, True, False, False, True])
    result, count = choose_representatives(ids, days, vectors, preferred, leaders)
    assert result.tolist() == [40, 40, 40, 40, 50, 50, -1] and count == 1
    preferred[3] = False
    result, count = choose_representatives(ids, days, vectors, preferred, leaders)
    assert result.tolist() == [10, 10, 10, 10, 50, 50, -1] and count == 1
    days[1] = 0
    assert choose_representatives(ids, days, vectors, preferred, leaders)[0][0] == 20
    preferred[0] = True
    assert choose_representatives(ids, days, vectors, preferred, leaders)[0][0] == 30
    assert choose_representatives(ids[:0], days[:0], vectors[:0], preferred[:0], leaders[:0])[1] == 0
    # 정규화 허용 오차가 있어도 내적이 아닌 코사인으로 비교한다.
    assert choose_representatives(np.array([1, 2, 3]), np.array([1, 1, 1]),
                                  np.array([[1, 0], [0.99999, 0], [1, 0.001]], dtype=np.float32),
                                  np.array([False, True, True]), np.array([1, 1, 1]))[0][0] == 2
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(20, "동일 기사 요약"), (10, "새 묶음에 포함되지 않은 기사 요약")]
    assert replace_groups(conn, np.array([30, 20, 10]), np.array([30, 30, -1])) == 2
    assert cursor.execute.call_args_list == [
        call("LOCK TABLE article_groups IN EXCLUSIVE MODE"),
        call("SELECT article_id, summary FROM article_groups WHERE summary IS NOT NULL"),
        call("DELETE FROM article_groups"),
    ]
    assert cursor.copy.return_value.__enter__.return_value.write_row.call_args_list == [
        call((20, 30, False, "동일 기사 요약")), call((30, 30, True, None))]
    print("자체점검=13", flush=True)


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("YYYY-MM-DD 날짜가 필요합니다.") from None


def positive(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path,
                        default=Path(os.environ.get("BIGKINDS_ENV_FILE", str(DEFAULT_ENV))),
                        help="접속 설정 파일 (BIGKINDS_DSN 우선)")
    parser.add_argument("--start", type=iso_date, help="시작 날짜 포함")
    parser.add_argument("--end", type=iso_date, help="끝 날짜 포함")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 통계만 출력")
    parser.add_argument("--block-size", type=positive, default=2048, help="GPU 행렬 타일 한 변의 최대 기사 수")
    parser.add_argument("--self-test", action="store_true", help="DB/CUDA 없이 창 선택 assert 점검")
    args = parser.parse_args()
    if args.start and args.end and args.start > args.end:
        parser.error("시작 날짜가 끝 날짜보다 늦습니다.")
    try:
        if args.self_test:
            self_test()
            return 0
        with psycopg.connect(**connection_options(args.env_file), autocommit=True,
                              connect_timeout=10, application_name="article_event_grouper") as conn:
            group_events(conn, args.start, args.end, args.dry_run, args.block_size)
    except LoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("묶음 계산 중단: 진행 중인 교체는 롤백됩니다.", file=sys.stderr)
        return 130
    except Exception:
        # 원본 DB/COPY/CUDA 예외에는 벡터나 접속값이 포함될 수 있다.
        print("묶음 계산 실패: CUDA·임시 저장 공간·DB 연결·권한·스키마를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
