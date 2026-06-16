"""CSV → MySQL 일괄 적재기.

사용법:
    python -m db.load_csv_to_db                  # meta_ml + meta2 모두
    python -m db.load_csv_to_db --only meta_ml   # 하나만
    python -m db.load_csv_to_db --mode append    # append 모드 (기본은 .env 의 LOAD_MODE)

LOAD_MODE=replace 면 테이블 drop → create → insert.
LOAD_MODE=append 면 기존 테이블에 누적 (테이블 없으면 create).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path
from typing import Iterable

import pandas as pd
from sqlalchemy import inspect, text

# 패키지/스크립트 양쪽 모두에서 동작
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.config import get_chunk_size, get_engine, get_load_mode  # noqa: E402
    from db.schema import META, TABLES, create_all, drop_all  # noqa: E402
else:
    from .config import get_chunk_size, get_engine, get_load_mode
    from .schema import META, TABLES, create_all, drop_all

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "meta_ml": PROJECT_ROOT / "meta_ml" / "output" / "서울특별시",
    "meta2":   PROJECT_ROOT / "meta2"   / "output" / "서울특별시",
}


# ---- CSV 읽기 + 전처리 ------------------------------------------------------

def _iter_csv_paths(root: Path) -> Iterable[Path]:
    """root/{구}/{동}/*.csv 만 수집."""
    pattern = str(root / "*" / "*" / "*.csv")
    for p in glob.iglob(pattern):
        yield Path(p)


def _read_csv_with_path(path: Path, table_name: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    # 스키마 컬럼만 사용 + 부족한 것은 NaN 으로 채움 (CSV 파일별 컬럼 변동 대비)
    schema_cols = [c.name for c in TABLES[table_name].columns
                   if c.name not in ("src_path", "ingested_at")]
    for c in schema_cols:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[schema_cols].copy()

    # 날짜 정규화
    if table_name == "meta_ml":
        df["Header_Timestamp"] = pd.to_datetime(df["Header_Timestamp"], errors="coerce").dt.date
    else:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce").dt.date

    df["src_path"] = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return df


# ---- 적재 -------------------------------------------------------------------

def _prepare_table(engine, table_name: str, mode: str) -> None:
    insp = inspect(engine)
    exists = insp.has_table(table_name)
    if mode == "replace":
        if exists:
            drop_all(engine, only=[table_name])
        create_all(engine, only=[table_name])
        print(f"[db] '{table_name}' 테이블 (재)생성")
    else:  # append
        if not exists:
            create_all(engine, only=[table_name])
            print(f"[db] '{table_name}' 신규 생성 (append)")
        else:
            print(f"[db] '{table_name}' 기존 테이블에 append")


def _load_one_table(engine, table_name: str, mode: str, chunk_rows: int) -> None:
    src_root = SOURCES[table_name]
    if not src_root.exists():
        print(f"[skip] {table_name}: {src_root} 없음")
        return

    _prepare_table(engine, table_name, mode)

    paths = sorted(_iter_csv_paths(src_root))
    n_files = len(paths)
    print(f"[load] {table_name}: {n_files:,} CSV → DB 적재 시작")

    t0 = time.perf_counter()
    buffer: list[pd.DataFrame] = []
    buffered_rows = 0
    total_rows = 0
    last_report = t0

    def _flush():
        nonlocal buffer, buffered_rows, total_rows
        if not buffer:
            return
        big = pd.concat(buffer, ignore_index=True)
        big.to_sql(
            table_name,
            con=engine,
            if_exists="append",
            index=False,
            chunksize=chunk_rows,
            method="multi",
        )
        total_rows += len(big)
        buffer = []
        buffered_rows = 0

    for i, p in enumerate(paths, 1):
        try:
            df = _read_csv_with_path(p, table_name)
        except Exception as e:
            print(f"  [warn] {p}: {e}")
            continue
        buffer.append(df)
        buffered_rows += len(df)

        # 5000행마다 flush (Railway 프록시 패킷 크기 보호)
        if buffered_rows >= 5000:
            _flush()

        now = time.perf_counter()
        if now - last_report >= 5.0:
            elapsed = now - t0
            rate = i / max(elapsed, 1e-6)
            print(f"  [{i:>6,}/{n_files:,}] {rate:>5.1f} files/s  rows={total_rows:,}")
            last_report = now

    _flush()
    print(
        f"[done] {table_name}: 파일 {n_files:,}개, 행 {total_rows:,}건 "
        f"({time.perf_counter() - t0:.1f}s)"
    )


# ---- 엔트리 -----------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="CSV → MySQL 적재")
    ap.add_argument(
        "--only", choices=list(TABLES.keys()), default=None,
        help="단일 테이블만 적재 (생략 시 모두)",
    )
    ap.add_argument(
        "--mode", choices=["replace", "append"], default=None,
        help="LOAD_MODE 오버라이드 (생략 시 .env 사용)",
    )
    ap.add_argument("--chunk", type=int, default=None, help="INSERT chunk size (생략 시 .env)")
    args = ap.parse_args()

    mode = args.mode or get_load_mode()
    chunk = args.chunk or get_chunk_size()
    engine = get_engine()

    with engine.connect() as c:
        v = c.execute(text("SELECT VERSION()")).scalar()
        print(f"[db] MySQL {v} | mode={mode} | chunk={chunk}")

    targets = [args.only] if args.only else list(TABLES.keys())
    for name in targets:
        _load_one_table(engine, name, mode=mode, chunk_rows=chunk)


if __name__ == "__main__":
    main()
