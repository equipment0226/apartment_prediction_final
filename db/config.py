"""DB 설정 + SQLAlchemy 엔진 팩토리.

`db/.env` 의 환경변수를 로드해 어디서든 동일한 커넥션을 얻도록 한다.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DB_DIR.parent

# db/.env 가 우선, 없으면 프로젝트 루트 .env 도 보조로 시도
load_dotenv(DB_DIR / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env", override=False)


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 환경변수가 없습니다. db/.env 를 확인하세요."
        )
    return url


def get_load_mode() -> str:
    mode = os.getenv("LOAD_MODE", "replace").strip().lower()
    if mode not in ("replace", "append"):
        raise ValueError(f"LOAD_MODE 는 replace|append 만 지원: {mode}")
    return mode


def get_chunk_size() -> int:
    return int(os.getenv("CHUNK_SIZE", "1000"))


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """프로세스 내 단일 엔진 (커넥션 풀 공유)."""
    return create_engine(
        get_database_url(),
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


if __name__ == "__main__":
    eng = get_engine()
    with eng.connect() as c:
        from sqlalchemy import text
        v = c.execute(text("SELECT VERSION()")).scalar()
        print(f"[db] connected. MySQL version = {v}")
