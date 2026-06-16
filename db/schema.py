"""테이블 스키마 정의 — meta_ml, meta2 (각각 1개 통합 테이블).

CSV 컬럼명을 그대로 보존하여 ipynb 파이프라인의 컬럼 참조가 동일하게 동작하도록 한다.
폴더 구조(시/구/동)는 행 데이터에 이미 포함되어 있으므로 별도 인덱스로 보존된다.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)

# utf8mb4 한글 컬럼명 사용. DDL 시 charset 명시.
META = MetaData()

# ---- meta_ml (ML 학습 패널) ---------------------------------------------------
# CSV 컬럼: Header_시/구/동/Timestamp/단지명/평형, target,
#           Static__*, depth1__*, depth2__*, depth3__*
META_ML = Table(
    "meta_ml",
    META,
    Column("Header_시", String(32), nullable=False),
    Column("Header_구", String(32), nullable=False),
    Column("Header_동", String(64), nullable=False),
    Column("Header_Timestamp", Date, nullable=False),
    Column("Header_단지명", String(128), nullable=False),
    Column("Header_평형", String(32), nullable=False),
    Column("target", Float),
    # Static (단지 정적 — 카테고리)
    Column("Static__준공구분", String(32)),
    Column("Static__세대수구분", String(32)),
    Column("Static__평수구분", String(32)),
    Column("Static__건설사등급", String(32)),
    Column("Static__초품아여부", String(32)),
    Column("Static__역세권수", Integer),
    Column("Static__호재수", Integer),
    # depth1 거시 (ECOS)
    Column("depth1__ecos__base_rate", Float),
    Column("depth1__ecos__cd_91d_rate", Float),
    Column("depth1__ecos__cpi_housing", Float),
    Column("depth1__ecos__m2_avg", Float),
    Column("depth1__ecos__mortgage_rate_new", Float),
    Column("depth1__ecos__unemployment_rate", Float),
    # depth2 권역 수급 (REB)
    Column("depth2__reb__apt_sale_supply_demand", Float),
    Column("depth2__reb__apt_jeonse_supply_demand", Float),
    Column("depth2__reb__apt_monthly_rent_supply_demand", Float),
    # depth3 구별 지수 + 정책
    Column("depth3__reb__apt_sale_index", Float),
    Column("depth3__reb__apt_jeonse_index", Float),
    Column("depth3__policy__ltv_tightness", Integer),
    Column("depth3__policy__dsr_severity", Integer),
    Column("depth3__policy__is_speculative", Integer),
    Column("depth3__policy__is_overheated", Integer),
    Column("depth3__policy__is_regulated", Integer),
    # 메타데이터
    Column("src_path", String(512), nullable=False),
    Column("ingested_at", DateTime, server_default=func.now()),
    Index(
        "ix_meta_ml_panel",
        "Header_구", "Header_동", "Header_단지명", "Header_평형", "Header_Timestamp",
    ),
    Index("ix_meta_ml_ts", "Header_Timestamp"),
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)


# ---- meta2 (메타데이터/시세 원본 패널) ----------------------------------------
# CSV 컬럼: Timestamp, 시, 구, 동, 아파트명, 전용면적, 시세,
#           준공년도, 세대수, 건설사, 초등학교, 인근역, 철도호재, 개발호재
META2 = Table(
    "meta2",
    META,
    Column("Timestamp", Date, nullable=False),
    Column("시", String(32), nullable=False),
    Column("구", String(32), nullable=False),
    Column("동", String(64), nullable=False),
    Column("아파트명", String(128), nullable=False),
    Column("전용면적", String(32), nullable=False),
    Column("시세", Float),
    Column("준공년도", Integer),
    Column("세대수", Integer),
    Column("건설사", String(128)),
    Column("초등학교", Text),
    Column("인근역", Text),
    Column("철도호재", Text),
    Column("개발호재", Text),
    Column("src_path", String(512), nullable=False),
    Column("ingested_at", DateTime, server_default=func.now()),
    Index(
        "ix_meta2_panel",
        "구", "동", "아파트명", "전용면적", "Timestamp",
    ),
    Index("ix_meta2_ts", "Timestamp"),
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)


TABLES = {"meta_ml": META_ML, "meta2": META2}


def create_all(engine, only: list[str] | None = None) -> None:
    if only is None:
        META.create_all(engine)
    else:
        META.create_all(engine, tables=[TABLES[n] for n in only])


def drop_all(engine, only: list[str] | None = None) -> None:
    if only is None:
        META.drop_all(engine)
    else:
        META.drop_all(engine, tables=[TABLES[n] for n in only])
